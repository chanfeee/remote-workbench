"""Fixed, stdlib-only text reads shared by both remote worker paths."""
import base64
import hashlib
import json
import os
from pathlib import Path
import stat
import sys


def read_lines(request):
    path = Path(request["path"]).expanduser()
    if request.get('for_edit'):
        path = path.absolute()
        if any(part.casefold() == '.git' for part in path.parts):
            return {'error': 'protected_git_path', 'path': str(path),
                    'message': 'Git metadata inside .git cannot be selected for editing.'}
        for component in [*reversed(path.parents), path]:
            if component.is_symlink():
                return {'error': 'edit_symlink_path', 'path': str(path),
                        'symlink_component': str(component),
                        'message': 'Edit snapshots do not follow symbolic links; select the actual file path explicitly.'}
        if path.resolve() != path:
            return {'error': 'edit_path_not_canonical', 'path': str(path),
                    'resolved_path': str(path.resolve()),
                    'message': 'Select the canonical file path before requesting an edit snapshot.'}
        try:
            info = path.stat()
        except FileNotFoundError:
            return {'error': 'file_not_found', 'path': str(path),
                    'message': 'File does not exist. Use apply_patch with an Add File directive.'}
        if not stat.S_ISREG(info.st_mode):
            return {'error': 'not_regular_file', 'path': str(path),
                    'message': 'Edit snapshots require a regular file; the selected path is a directory or another file type.'}
    maximum = request.get("max_file_size", 10485760)
    if path.stat().st_size > maximum:
        raise ValueError("File exceeds configured size limit")
    # Hash and selected lines refer to exactly the same bounded snapshot.
    with path.open("rb") as source:
        snapshot = source.read(maximum + 1)
    if len(snapshot) > maximum:
        raise ValueError("File exceeds configured size limit")
    digest = hashlib.sha256(snapshot).hexdigest()
    lines = snapshot.decode("utf-8").splitlines(keepends=True)
    selected = []
    total = len(lines)
    used = 0
    truncated = False
    next_offset = request["offset"]
    for index in range(request["offset"], min(total, request["offset"] + request["limit"])):
        value = f"{index+1}\t{lines[index]}"
        size = len(value.encode("utf-8"))
        if used + size > request["budget"]:
            if not selected:
                return {"error": "line_exceeds_budget", "line": index+1, "required_bytes": size, "sha256": digest}
            truncated = True
            break
        selected.append(value)
        used += size
        next_offset = index + 1
    result = {"content": "".join(selected).rstrip("\n"), "total_lines": total,
            "sha256": digest, "next_offset": next_offset,
            "truncated": truncated or total > next_offset}
    if request.get('for_edit'):
        # A workspace/edit root is a directory, not a repository. Files outside
        # the current cwd can still be explicitly selected by their full path.
        edit_root = Path.cwd()
        if not path.is_relative_to(edit_root):
            edit_root = path.parent
        result.update(_edit_data_base64=base64.b64encode(snapshot).decode('ascii'),
                      _edit_path=str(path), _edit_repository=str(edit_root))
    return result


def read_selected(request):
    """One bounded, explicit selection; never discover or scan a project."""
    selection = request.get('files')
    if (not isinstance(selection, list) or not 1 <= len(selection) <= 16
            or any(not isinstance(item, dict) or not isinstance(item.get('path'), str)
                   or not item['path'] or '\x00' in item['path']
                   or type(item.get('for_edit')) is not bool for item in selection)):
        raise ValueError('Select 1..16 explicit files')
    results, used = [], 0
    snapshot_budget = 4 * ((2 * request.get('max_file_size', 10485760) + 2) // 3)
    for item in selection:
        try:
            if '.remote-workbench-sessions' in Path(os.path.abspath(item['path'])).parts:
                raise ValueError('Select files from the actual directory, not a managed project copy')
            value = read_lines(dict(request, path=item['path'], for_edit=item['for_edit']))
            size = len(value.get('_edit_data_base64', ''))
            if used + size > snapshot_budget:
                value = {'error': 'snapshot_batch_budget', 'message': 'Read this file individually; selected snapshots exceed the batch budget'}
            else:
                used += size
        except (OSError, ValueError, UnicodeError) as exc:
            value = {'error': 'read_failed', 'message': str(exc)[:512]}
        results.append(value)
    return {'files': results}


def read_main():
    request = json.load(sys.stdin)
    operations = {"read_lines": read_lines, "read_selected": read_selected}
    try:
        result = operations[request["op"]](request)
    except (KeyError, OSError, ValueError) as exc:
        result = {"error": "remote_operation_failed", "message": str(exc)}
    marker = "\x1eREMOTE_WORKBENCH_RESULT_" + request["_response_id"] + ":\x1f"
    print(marker + json.dumps(result, ensure_ascii=False))
