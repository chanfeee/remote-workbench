"""Explicit per-file edit snapshots; no project mirroring or guessed unescaping."""
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile

from remote_workbench import audit
from remote_workbench.edit_spec import validate_prepared_edits
from remote_workbench.native_patch import prepared_file_patch


def _origin(executor):
    meta = audit.current_metadata()
    return {**{k: meta.get(k) for k in ('workspace_id', 'target_id')},
            'endpoint': executor.config.ssh_target, 'cwd': executor.config.default_cwd,
            'state_dir': executor.remote_state_dir}


def _read(path, maximum):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as source:
        before = os.fstat(source.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size > maximum:
            raise ValueError('Edit snapshot must be a bounded regular file')
        data = source.read(maximum + 1)
        after = os.fstat(source.fileno())
    if len(data) > maximum or (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise ValueError('Local edit file changed while being read')
    return data


def save(executor, result, local_copy=True):
    encoded = result.pop('_edit_data_base64')
    if len(encoded) > 4 * ((executor.config.max_file_size + 2) // 3):
        raise ValueError('Edit snapshot exceeds the file size limit')
    data = base64.b64decode(encoded, validate=True)
    remote = result.pop('_edit_path')
    repository = result.pop('_edit_repository')
    if len(data) > executor.config.max_file_size or hashlib.sha256(data).hexdigest() != result['sha256']:
        raise ValueError('Invalid edit snapshot')
    relative = Path(remote).relative_to(repository)
    if not relative.parts or '..' in relative.parts or any(ord(c) < 32 for c in remote):
        raise ValueError('Invalid edit path')
    directory = audit.directory(result['output_id']) / 'edit'
    directory.mkdir(mode=0o700)
    suffix = Path(remote).suffix
    working = 'working' + (suffix if re.fullmatch(r'\.[A-Za-z0-9]{1,16}', suffix) else '.txt')
    # OS temporary storage is writable by local agent tools without granting
    # access to the private persistent registry directory.
    staging = Path(tempfile.mkdtemp(prefix='rwb-edit-' + result['output_id'] + '-')) if local_copy else None
    manifest = {'origin': _origin(executor), 'path': remote, 'repository': repository,
                'sha256': result['sha256'], 'working': working, 'staging': str(staging) if staging else None}
    files = [(directory / 'original', data), (directory / 'manifest.json', json.dumps(manifest).encode())]
    if staging is not None:
        files.append((staging / working, data))
    for destination, content in files:
        fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'wb') as output:
            output.write(content)
    result.update(edit_id=result['output_id'], path=remote)
    if staging is not None:
        result['local_path'] = str(staging / working)
    return result


def materialize(executor, edits):
    validate_prepared_edits(edits)
    grouped = {}
    for edit in edits:
        grouped.setdefault(edit['edit_id'], []).append(edit)
    originals, contents, guards, repository = {}, {}, {}, None
    total_bytes = 0
    for edit_id, changes in grouped.items():
        record = audit.directory(edit_id)
        directory = record / 'edit'
        if record.is_symlink() or directory.is_symlink() or record.resolve().parent != audit.root().resolve():
            raise ValueError('Edit snapshot is outside this workspace')
        try:
            manifest = json.loads(_read(directory / 'manifest.json', 32768))
        except FileNotFoundError as exc:
            raise ValueError('Unknown edit_id in this workspace; use read_file(for_edit=true)') from exc
        if manifest['origin'] != _origin(executor):
            raise ValueError('Edit snapshot belongs to another target; switch back or read the selected target')
        if not re.fullmatch(r'working(?:\.[A-Za-z0-9]{1,16})?', manifest['working']):
            raise ValueError('Invalid local edit filename')
        before = _read(directory / 'original', executor.config.max_file_size)
        if hashlib.sha256(before).hexdigest() != manifest['sha256']:
            raise ValueError('Original edit snapshot changed')
        path, repo = manifest['path'], manifest['repository']
        if repository is not None and repository != repo:
            raise ValueError('Apply prepared edits from one Git repository per call')
        repository = repo
        relative = Path(path).relative_to(repo)
        if not relative.parts or '..' in relative.parts or '.git' in {p.casefold() for p in relative.parts} or path in guards:
            raise ValueError('Invalid or repeated prepared path')
        guards[path] = manifest['sha256']
        if any(set(item) == {'edit_id', 'new'} for item in changes):
            if len(changes) != 1:
                raise ValueError('Whole-file replacement cannot be mixed with other edits for the same snapshot')
            newline = '\r\n' if b'\r\n' in before and b'\n' not in before.replace(b'\r\n', b'') else '\n'
            text = newline.join(changes[0]['new'])
            if changes[0]['new'] and (not before or before.endswith(b'\n')):
                text += newline
            after = text.encode('utf-8')
        elif any(set(item) == {'edit_id'} for item in changes):
            if len(changes) != 1:
                raise ValueError('Do not mix a working file with line edits for the same edit_id')
            if manifest['staging'] is None:
                raise ValueError('Use edit_id/new for this snapshot; no local copy was requested')
            staging = Path(manifest['staging'])
            if (staging.is_symlink() or staging.parent.resolve() != Path(tempfile.gettempdir()).resolve()
                    or not staging.name.startswith('rwb-edit-' + edit_id + '-')
                    or staging.stat().st_uid != os.getuid() or stat.S_IMODE(staging.stat().st_mode) != 0o700):
                raise ValueError('Invalid local edit staging directory')
            after = _read(staging / manifest['working'], executor.config.max_file_size)
        else:
            lines = before.decode('utf-8').splitlines(keepends=True)
            ranges = sorted(changes, key=lambda e: e['start_line'])
            previous = 0
            newline = '\r\n' if b'\r\n' in before and b'\n' not in before.replace(b'\r\n', b'') else '\n'
            for item in ranges:
                if set(item) == {'edit_id'} or item['start_line'] <= previous or item['end_line'] > len(lines):
                    raise ValueError('Prepared line ranges must be within the snapshot and not overlap')
                previous = item['end_line']
            for item in reversed(ranges):
                start, end = item['start_line'] - 1, item['end_line']
                replacement = newline.join(item['new'])
                if item['new'] and (end < len(lines) or before.endswith(b'\n')):
                    replacement += newline
                lines[start:end] = [replacement]
            after = ''.join(lines).encode('utf-8')
        if len(after) > executor.config.max_file_size:
            raise ValueError('Prepared result exceeds the file size limit')
        total_bytes += len(before) + len(after)
        if total_bytes > 2 * executor.config.max_file_size:
            raise ValueError('Prepared batch exceeds the size budget; apply smaller selected batches')
        originals[path], contents[path] = before, after.decode('utf-8')
    patch = prepared_file_patch(originals, contents, repository)
    if len(patch.encode()) > 10485760:
        raise ValueError('Prepared patch exceeds 10 MiB')
    return {'patch': patch, 'expected_files': guards, 'expected_repository': repository,
            'prepared_contents': contents}


def normalize(arguments, executor):
    edits = arguments.pop('edits')
    arguments.update(materialize(executor, edits))


def applied_refs(executor, arguments, output_id):
    """Cache successfully applied bytes, without another SSH read or model round.

    A later command/external edit can make these references stale; the existing
    remote SHA guard remains authoritative on the next apply.
    """
    refs = []
    for path, content in arguments['prepared_contents'].items():
        data = content.encode('utf-8')
        edit_id = audit.create(kind='edit_snapshot', parent_id=output_id, path=path, status='finished')
        value = save(executor, {'output_id':edit_id, '_edit_data_base64':base64.b64encode(data).decode('ascii'),
            '_edit_path':path, '_edit_repository':arguments['expected_repository'],
            'sha256':hashlib.sha256(data).hexdigest()}, local_copy=False)
        refs.append({'path':path, 'edit_id':value['edit_id']})
    return refs
