"""Validate explicit remote directories without creating or copying projects."""
import json
import posixpath


def reject_managed_copy(path):
    if '.remote-workbench-sessions' in posixpath.normpath(path).split('/'):
        raise ValueError('A managed project copy is not a direct working directory; select the original source directory')


async def inspect_directory(executor, cwd):
    if not isinstance(cwd, str) or not cwd.startswith('/') or '\x00' in cwd:
        raise ValueError('cwd must be an absolute remote directory')
    reject_managed_copy(cwd)
    connected, message = await executor.check_connection()
    if not connected:
        raise ValueError(message)
    code = ("import json,os,sys\n"
            "p=os.path.realpath(sys.argv[1]); os.chdir(p)\n"
            "print(json.dumps({'cwd':os.getcwd(),'home':os.path.expanduser('~')}))")
    result = await executor.run_program(['python3', '-c', code, cwd], cwd='/', output_budget=2048)
    if result.exit_code:
        raise ValueError('Remote directory validation failed: ' + result.stderr)
    try:
        detail = json.loads(result.stdout)
        canonical = detail['cwd']
        if not isinstance(canonical, str) or not canonical.startswith('/'):
            raise ValueError('Remote directory was not absolute')
        reject_managed_copy(canonical)
        return detail
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError('Invalid remote directory validation result') from exc
