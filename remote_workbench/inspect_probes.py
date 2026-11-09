"""Fixed, bounded probes sent to the bound host as stdlib-only Python."""
from contextlib import contextmanager
from itertools import islice
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys


OUTPUT_LIMIT = 4096
FILE_LIMIT = 8 * 1024 * 1024
ENTRY_LIMIT = 128


def _checked_path(path):
    if (not path or '\x00' in path or path.startswith(('-', '~')) or len(os.fsencode(path)) > 4096
            or set(Path(path).parts) & {'..', '.remote-workbench-sessions'}):
        raise ValueError('Use an explicit path inside the bound directory')
    return path


def normalize_probe(argv):
    """Recognize the complete argv grammar and return only canonical probes."""
    program, *args = argv
    name = Path(program).name
    if program in {name, '/bin/' + name, '/usr/bin/' + name}:
        if name == 'pwd' and not args:
            return ['/bin/pwd']
        if (name == 'printf' and len(args) == 2 and args[0] in {'%s\\n', '%s\n'}
                and len(args[1].encode('utf-8')) <= 512):
            return ['/usr/bin/printf', *args]
        if name == 'ls' and len(args) <= 1:
            return ['ls', _checked_path(args[0]) if args else '.']
        if name == 'stat' and len(args) == 1:
            return ['stat', _checked_path(args[0])]
        if name == 'head':
            if len(args) == 1:
                return ['head', '-n', '10', _checked_path(args[0])]
            if len(args) == 3 and args[0] == '-n' and re.fullmatch(r'[1-9][0-9]{0,2}', args[1]):
                if int(args[1]) <= 100:
                    return ['head', '-n', args[1], _checked_path(args[2])]
        if name == 'wc' and len(args) == 2 and args[0] == '-l':
            return ['wc', '-l', _checked_path(args[1])]
        if name == 'git' and args:
            subcommand, *options = args
            if subcommand == 'diff':
                end = options.index('--') if '--' in options else len(options)
                revisions, paths = options[:end], options[end + 1:]
                if revisions == ['--staged']:
                    revisions = ['--cached']
                if ((revisions == ['--cached'] or
                        (len(revisions) == 2 and all(re.fullmatch(r'[0-9a-fA-F]{7,64}', r) for r in revisions)))
                        and len(paths) <= 8):
                    return ['git', 'diff', *revisions, '--', *map(_checked_path, paths)]
            if subcommand == 'log' and (not options or (
                    len(options) == 2 and options[0] == '-n'
                    and re.fullmatch(r'[1-9][0-9]?', options[1]) and int(options[1]) <= 20)):
                return ['git', 'log', '-n', options[1] if options else '10']
    raise ValueError('inspect_batch permits only documented read-only probes and fixed arguments')


@contextmanager
def _parent(path):
    target = Path(path)
    if target.is_absolute():
        target = target.relative_to(Path.cwd())
    parts = target.parts
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    descriptor = os.open('.', flags)
    try:
        for part in parts[:-1]:
            child = os.open(part, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        yield descriptor, parts[-1] if parts else '.'
    finally:
        os.close(descriptor)


@contextmanager
def _regular_file(parent, name):
    before = os.stat(name, dir_fd=parent, follow_symlinks=False)
    if not stat.S_ISREG(before.st_mode):
        raise ValueError('Select a regular file without symbolic links')
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
    with os.fdopen(fd, 'rb') as source:
        actual = os.fstat(source.fileno())
        if not stat.S_ISREG(actual.st_mode) or not os.path.samestat(before, actual):
            raise ValueError('File changed while it was being opened')
        yield source


def _print_bytes(value):
    sys.stdout.buffer.write(value[:OUTPUT_LIMIT])
    if len(value) > OUTPUT_LIMIT:
        sys.stdout.buffer.write(b'\n[output truncated]\n')


def _list_entries(parent, name):
    fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
    try:
        with os.scandir(fd) as entries:
            names = [entry.name for entry in islice(entries, ENTRY_LIMIT + 1)]
    finally:
        os.close(fd)
    visible = []
    remaining = OUTPUT_LIMIT - 64  # Reserve space for the JSON object and flag.
    for name in sorted(names[:ENTRY_LIMIT]):
        remaining -= len(json.dumps(name)) + 2
        if remaining < 0:
            break
        visible.append(name)
    print(json.dumps({'entries': visible, 'truncated': len(visible) < len(names)}))


def _inspect_file(program, args):
    path = args[-1]
    with _parent(path) as (parent, name):
        if program == 'ls':
            _list_entries(parent, name)
        elif program == 'stat':
            value = os.stat(name, dir_fd=parent, follow_symlinks=False)
            print(json.dumps({'mode': stat.filemode(value.st_mode), 'size': value.st_size,
                              'mtime_ns': value.st_mtime_ns}))
        else:
            with _regular_file(parent, name) as source:
                if program == 'head':
                    result = bytearray()
                    for _ in range(int(args[1])):
                        line = source.readline(OUTPUT_LIMIT + 1 - len(result))
                        result.extend(line)
                        if not line or len(result) > OUTPUT_LIMIT:
                            break
                    _print_bytes(result)
                else:
                    remaining = FILE_LIMIT
                    lines = 0
                    while chunk := source.read(min(65536, remaining + 1)):
                        remaining -= len(chunk)
                        if remaining < 0:
                            raise ValueError('File exceeds the 8 MiB inspection limit')
                        lines += chunk.count(b'\n')
                    _print_bytes(os.fsencode(f'{lines}\t{path}\n'))


def _git_probe(args):
    env = {key: value for key, value in os.environ.items() if not key.startswith('GIT_')}
    env.update(PATH='/usr/bin:/bin', GIT_OPTIONAL_LOCKS='0', GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull,
               GIT_TERMINAL_PROMPT='0', GIT_NO_LAZY_FETCH='1', GIT_ALLOW_PROTOCOL='')
    prefix = ['/usr/bin/git', '--no-pager', '--literal-pathspecs', '--git-dir=.git', '--work-tree=.',
              '-c', 'core.fsmonitor=false', '-c', 'core.hooksPath=/dev/null']
    if args[0] == 'diff':
        options = ['diff', '--no-ext-diff', '--no-textconv', '--no-renames', '--ignore-submodules=all',
                   '--no-color', *args[1:]]
    else:
        options = ['log', '--no-show-signature', '--no-decorate', '--no-notes',
                   '--format=%h %s', *args[1:], '--']
    with subprocess.Popen([*prefix, *options], stdin=subprocess.DEVNULL,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env) as process:
        value = process.stdout.read(OUTPUT_LIMIT + 1)
        _print_bytes(value)
        if len(value) > OUTPUT_LIMIT:
            process.terminate()
            return 0
        return process.wait()


def main(argv):
    try:
        program, *args = normalize_probe(argv)
        if program == 'git':
            return _git_probe(args)
        if program not in {'ls', 'stat', 'head', 'wc'}:
            raise ValueError('Unsupported filesystem probe')
        _inspect_file(program, args)
        return 0
    except (OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))
