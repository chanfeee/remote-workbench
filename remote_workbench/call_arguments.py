"""Small local argument checks and data forms, not a shell-language parser."""
import shlex
from remote_workbench.command_spec import validate_batch

from remote_workbench.native_patch import validate_native_patch_syntax
from remote_workbench.edit_spec import edits_patch, prepared_edits, validate_prepared_edits


def command_text(command):
    """Readable command for review/audit; argv execution uses program_command."""
    if isinstance(command, dict):
        validate_batch(command)
        return '\n'.join(f'{index + 1}: ' + shlex.join(argv) for index, argv in enumerate(command['commands']))
    if isinstance(command, list):
        if (not command or not all(isinstance(v, str) and "\x00" not in v for v in command)
                or not command[0]):
            raise ValueError("command array must contain strings without NUL and a nonempty program")
        return shlex.join(command)
    if not isinstance(command, str) or not command.strip() or "\x00" in command:
        raise ValueError("command must be nonempty shell text or an array of program arguments")
    return command


def patch_text(patch):
    if isinstance(patch, list):
        if not patch or not all(isinstance(line, str) and '\n' not in line and '\r' not in line for line in patch):
            raise ValueError("patch array must contain individual string lines; omit embedded line breaks")
        patch = '\n'.join(patch)
        if not patch.endswith('\n'):
            patch += '\n'
    if not isinstance(patch, str) or not patch.strip() or "\x00" in patch or len(patch.encode()) > 10485760:
        raise ValueError("patch must contain 1..10485760 UTF-8 bytes, as text or an array of lines")
    if '\n' not in patch and '\\n' in patch:
        raise ValueError('patch contains literal \\n; pass real line breaks or an array of lines')
    if patch.startswith('*** Begin Patch') and patch.splitlines()[-1] != '*** End Patch':
        raise ValueError('context patch must end with *** End Patch')
    if patch.startswith('*** Begin Patch'):
        validate_native_patch_syntax(patch)
    return patch


def validate_call_arguments(name, arguments):
    """Run before SSH, job submission or a managed-directory claim."""
    if name not in {'exec_command', 'apply_patch'}:
        return
    if name == 'exec_command':
        command_text(arguments.get('command'))
        timeout = arguments.get('timeout')
        if timeout is not None and (type(timeout) is not int or timeout <= 0):
            raise ValueError('timeout must be a positive integer number of seconds or null')
        budget = arguments.get('max_bytes')
        if budget is not None and (type(budget) is not int or not 256 <= budget <= 1048576):
            raise ValueError('max_bytes must be an integer between 256 and 1048576')
    else:
        if arguments.get('edits') is not None:
            if arguments.get('patch') is not None:
                raise ValueError('Pass edits or patch, not both')
            if prepared_edits(arguments['edits']):
                validate_prepared_edits(arguments['edits'])
            else:
                arguments['patch'] = edits_patch(arguments.pop('edits'))
        if arguments.get('edits') is None:
            if arguments.get('patch') != '' or not arguments.get('expected_files'):
                arguments['patch'] = patch_text(arguments.get('patch'))
        guards = arguments.get('expected_files')
        if guards is not None:
            import re
            if not isinstance(guards, dict) or not 1 <= len(guards) <= 100 or any(
                    not isinstance(p, str) or not p.startswith('/') or '\x00' in p or
                    not isinstance(h, str) or not re.fullmatch('[a-f0-9]{64}', h) for p, h in guards.items()):
                raise ValueError('expected_files must map absolute paths to SHA256 hashes')
    cwd = arguments.get('cwd')
    if cwd is not None and (not isinstance(cwd, str) or '\x00' in cwd or cwd.startswith('~')):
        raise ValueError('cwd must be absolute or workspace-relative; expand ~ to an absolute path')
