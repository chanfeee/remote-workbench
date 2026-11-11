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
    try:
        detail = await executor.probe_directory(cwd)
        canonical = detail['cwd']
        if not isinstance(canonical, str) or not canonical.startswith('/'):
            raise ValueError('Remote directory was not absolute')
        reject_managed_copy(canonical)
        return detail
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError('Invalid remote directory validation result') from exc
