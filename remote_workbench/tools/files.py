"""File operation tools."""

from __future__ import annotations

import os
import shutil
import tempfile
from remote_workbench.remote_client import edit as remote_edit

from remote_workbench.server import mcp, require_connection


@mcp.tool()
async def ssh_read_file(path: str | list[str], offset: int = 0, limit: int = 160, host: str | None = None, max_bytes: int = 8192,
                        for_edit: bool = False, local_copy: bool = True) -> dict:
    """Read numbered lines from one path or up to 16 explicit paths in one SSH request. for_edit returns per-file edit_id snapshots; no project scan. offset is 0-based; max_bytes is per file."""
    if offset < 0 or not 1 <= limit <= 2000:
        raise ValueError("offset >= 0 and 1 <= limit <= 2000 required")
    if isinstance(path, list):
        if (not 1 <= len(path) <= 16
                or any(not isinstance(p, str) or not p or '\x00' in p or p.startswith('~') for p in path)
                or len(set(path)) != len(path)):
            raise ValueError('Select 1..16 distinct explicit paths without NUL or ~')
        executor = await require_connection(host)
        return {'files': await read_selected(executor, [(p, for_edit) for p in path],
            max_bytes=max_bytes, offset=offset, limit=limit, local_copy=local_copy)}
    executor = await require_connection(host)
    result = await remote_edit(executor, {"op": "read_lines", "path": path, "offset": offset, "limit": limit,
                                         "budget": max_bytes, "for_edit": for_edit}, max_bytes=max_bytes)
    if for_edit and not result.get('error'):
        from remote_workbench.prepared_edit import save
        result = save(executor, result, local_copy=local_copy)
    return result


async def read_selected(executor, selection, *, max_bytes=4096, offset=0, limit=160, local_copy=False, cwd=None):
    """Internal bind/read combination; one SSH request, separate local records."""
    from remote_workbench import audit
    from remote_workbench.prepared_edit import save
    from remote_workbench.tool_interface import present_result
    connected, message = await executor.check_connection()
    if not connected:
        raise ValueError(message)
    options = {'max_bytes': max_bytes}
    if cwd is not None:
        options['cwd'] = cwd
    response = await remote_edit(executor, {'op': 'read_selected',
        'files': [{'path':path, 'for_edit':for_edit} for path, for_edit in selection],
        'offset':offset, 'limit':limit, 'budget':max_bytes}, **options)
    values = response.get('files')
    if not isinstance(values, list) or len(values) != len(selection):
        return [dict(response, error=response.get('error', 'invalid_read_batch'), requested_path=path)
                for path, _ in selection]
    results = []
    for (path, for_edit), value in zip(selection, values):
        if not isinstance(value, dict):
            value = {'error': 'invalid_read_result'}
        record = audit.create(kind='tool', tool='ssh_read_file', parent_id=audit.operation_id.get(),
            host=executor.config.ssh_target, cwd=cwd if cwd is not None else executor.config.default_cwd,
            arguments={'path':path, 'for_edit':for_edit}, source_output_id=response.get('output_id'),
            status='failed' if value.get('error') else 'finished')
        audit.add_child(audit.operation_id.get(), record)
        value = dict(value, output_id=record)
        try:
            if for_edit and not value.get('error'):
                value = save(executor, value, local_copy=local_copy)
        except (OSError, ValueError, KeyError) as exc:
            value = {'error':'read_failed', 'message':str(exc)[:512], 'output_id':record}
        audit.update(record, status='failed' if value.get('error') else 'finished',
                     error=value.get('error'), snapshot_available=bool(value.get('edit_id')))
        with audit.open_stream(record, 'stdout') as output:
            display = (value.get('message') or value['error']) if value.get('error') else value.get('content', '')
            output.write(display.encode('utf-8'))
        results.append(dict(present_result('read_file', value, {'path':path}), requested_path=path))
    return results


@mcp.tool()
async def ssh_write_file(path: str, content: str, create_dirs: bool = False, host: str | None = None, expected_sha256: str | None = None) -> dict:
    """Write UTF-8 content atomically on the remote host, preserving existing mode/owner."""
    executor = await require_connection(host)
    return await remote_edit(executor, {"op": "write", "path": path, "content": content, "create_dirs": create_dirs, "expected_sha256": expected_sha256})


@mcp.tool()
async def ssh_list_dir(
    path: str = ".",
    pattern: str = "*",
    recursive: bool = False,
    max_entries: int = 500,
    host: str | None = None,
) -> dict:
    """List directory contents on the remote server.

    Args:
        path: Directory path to list. Defaults to current working directory.
        pattern: Glob pattern to filter entries (e.g., "*.py", "Makefile").
        recursive: If true, list recursively.
        max_entries: Maximum entries to return (prevents overwhelming output).
        host: SSH host alias from config. Uses default if omitted.
    """
    executor = await require_connection(host)
    max_entries = int(max_entries)
    if not 1 <= max_entries <= 2000:
        raise ValueError("max_entries must be between 1 and 2000")

    if recursive:
        args = ["find", path, "-name", pattern, "-maxdepth", "10", "-exec", "ls", "-ld", "{}", "+"]
        limit = max_entries + 1
    else:
        # Use find instead of ls glob to avoid shell expansion of pattern
        if pattern == "*":
            args = ["ls", "-la", path]
            limit = max_entries + 2
        else:
            args = ["find", path, "-maxdepth", "1", "-name", pattern, "-exec", "ls", "-ld", "{}", "+"]
            limit = max_entries + 1

    result = await executor.run_program(args, path_indices=[2 if args[0] == "ls" else 1], max_lines=limit)

    if result.exit_code != 0:
        return {"error": "list_failed", "message": result.stderr.strip() or "Directory not found or empty"}

    lines = result.stdout.strip().split("\n") if result.stdout.strip() else []

    if recursive:
        # ls -ld output: just pass through the lines
        entries = []
        for line in lines[:max_entries]:
            if line.startswith("total "):
                continue
            entries.append(line)
        truncated = len(lines) > max_entries
    else:
        # ls -la output: skip the "total N" line
        entries = []
        for line in lines:
            if line.startswith("total "):
                continue
            entries.append(line)
        truncated = len(entries) > max_entries
        entries = entries[:max_entries]

    return {
        "entries": entries,
        "count": len(entries),
        "truncated": truncated or result.truncated,
        "output_id": result.output_id,
        "path": path,
    }


@mcp.tool()
async def ssh_file_info(
    path: str,
    host: str | None = None,
) -> dict:
    """Get detailed information about a file or directory on the remote server.

    Args:
        path: Path to inspect.
        host: SSH host alias from config. Uses default if omitted.
    """
    executor = await require_connection(host)

    # Try GNU stat first, fall back to BSD stat
    result = await executor.run_program(["stat", "--format=%F|%s|%U|%G|%A|%y", path], path_indices=[2])
    if result.exit_code != 0:
        result = await executor.run_program(["stat", "-f", "%HT|%z|%Su|%Sg|%Sp|%Sm", path], path_indices=[3])

    if result.exit_code != 0:
        return {"error": "not_found", "message": result.stderr.strip()}

    parts = result.stdout.strip().split("|")
    if len(parts) >= 6:
        return {
            "exists": True,
            "type": parts[0],
            "size": int(parts[1]) if parts[1].isdigit() else parts[1],
            "owner": parts[2],
            "group": parts[3],
            "permissions": parts[4],
            "modified": parts[5],
            "path": path,
        }

    return {
        "exists": True,
        "raw": result.stdout.strip(),
        "path": path,
    }


@mcp.tool()
async def ssh_patch_file(path: str, old: str | None = None, new: str | None = None, host: str | None = None,
                         edits: list[dict[str, str]] | None = None, expected_sha256: str | None = None,
                         check_only: bool = False, max_bytes: int = 8192) -> dict:
    """Apply unique old/new replacements remotely, sequentially, all-or-none per file.

    Supply either old/new or edits=[{old,new},...]. Optional expected_sha256 rejects
    stale content. Returns this operation's diff; full diff is saved by output_id.
    """
    if edits is not None and (old is not None or new is not None):
        raise ValueError("Use old/new OR edits")
    if edits is None:
        if old is None or new is None:
            raise ValueError("Both old and new are required")
        edits = [{"old": old, "new": new}]
    if not edits or any(set(item) != {"old", "new"} or not item["old"] or not all(isinstance(v, str) for v in item.values()) for item in edits):
        raise ValueError("edits must contain nonempty old strings and string new values")
    executor = await require_connection(host)
    return await remote_edit(executor, {"op": "replace", "path": path, "edits": edits,
        "expected_sha256": expected_sha256, "check_only": check_only}, max_bytes=max_bytes)


@mcp.tool()
async def ssh_mkdir(
    path: str,
    parents: bool = True,
    host: str | None = None,
) -> dict:
    """Create a directory on the remote server.

    Args:
        path: Directory path to create.
        parents: If true, create parent directories as needed (mkdir -p).
        host: SSH host alias from config. Uses default if omitted.
    """
    executor = await require_connection(host)
    args = ["mkdir", *(["-p"] if parents else []), path]
    result = await executor.run_program(args, path_indices=[len(args) - 1])

    if result.exit_code != 0:
        return {"error": "mkdir_failed", "message": result.stderr.strip()}
    return {"success": True, "path": path}


@mcp.tool()
async def ssh_move(
    source: str,
    dest: str,
    host: str | None = None,
) -> dict:
    """Move or rename a file/directory on the remote server.

    Args:
        source: Current path.
        dest: New path.
        host: SSH host alias from config. Uses default if omitted.
    """
    executor = await require_connection(host)
    result = await executor.run_program(["mv", source, dest], path_indices=[1, 2])

    if result.exit_code != 0:
        return {"error": "move_failed", "message": result.stderr.strip()}
    return {"success": True, "source": source, "dest": dest}


@mcp.tool()
async def ssh_delete(
    path: str,
    recursive: bool = False,
    host: str | None = None,
) -> dict:
    """Delete a file or directory on the remote server.

    Args:
        path: Path to delete.
        recursive: If true, delete directories recursively (rm -r).
        host: SSH host alias from config. Uses default if omitted.
    """
    executor = await require_connection(host)
    args = ["rm", *(["-r"] if recursive else []), path]
    result = await executor.run_program(args, path_indices=[len(args) - 1])

    if result.exit_code != 0:
        return {"error": "delete_failed", "message": result.stderr.strip()}
    return {"success": True, "path": path}


@mcp.tool()
async def ssh_fetch_image(path: str, page: int = 1, host: str | None = None) -> dict:
    """Fetch a remote plot to this chat's private local cache; return paths, never image/base64 data.

    PNG/JPG display directly. PDF (1-based page) and SVG get a local PNG preview
    when the optional images extra is installed. Show image_markdown in chat;
    use the native image-view tool on preview_path only when analysis is needed.
    """
    from remote_workbench.image_cache import fetch_image
    if isinstance(page, bool) or not isinstance(page, int) or page < 1:
        raise ValueError("page must be a positive integer")
    executor = await require_connection(host)
    return await fetch_image(executor, path, page)


@mcp.tool()
async def ssh_download(
    remote_path: str,
    local_dir: str = "",
    host: str | None = None,
) -> dict:
    """Download raw file bytes through the full spool, preserving binary content.

    Args:
        remote_path: Path to the file on the remote server.
        local_dir: Local directory to save to. Defaults to ~/Downloads.
        host: SSH host alias from config. Uses default if omitted.
    """
    executor = await require_connection(host)

    if not local_dir:
        local_dir = os.path.expanduser("~/Downloads")

    # Resolve to absolute path and ensure it's under home directory
    local_dir = os.path.realpath(os.path.expanduser(local_dir))
    home = os.path.realpath(os.path.expanduser("~"))
    if os.path.commonpath([local_dir, home]) != home:
        return {"error": "invalid_path", "message": f"local_dir must be under your home directory ({home})."}

    os.makedirs(local_dir, exist_ok=True)
    filename = os.path.basename(remote_path)
    if not filename or filename.startswith("."):
        return {"error": "invalid_filename", "message": "Remote path must have a non-hidden filename."}
    local_path = os.path.join(local_dir, filename)

    # Read via SSH and write locally
    result = await executor.run_program(["cat", remote_path], path_indices=[1], timeout=executor.config.file_timeout)

    if result.exit_code != 0:
        return {"error": "download_failed", "message": result.stderr.strip()}

    # Stream raw bytes into a private temporary file, then replace the local
    # target. Never follow a pre-existing destination symlink or save a preview.
    from remote_workbench import audit
    spool = audit.directory(result.output_id) / "stdout"
    fd, temporary = tempfile.mkstemp(prefix=".remote-workbench-download-", dir=local_dir)
    try:
        with os.fdopen(fd, "wb") as output, spool.open("rb") as source:
            shutil.copyfileobj(source, output)
        os.replace(temporary, local_path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)

    return {
        "success": True,
        "local_path": local_path,
        "size": spool.stat().st_size,
    }


@mcp.tool()
async def ssh_append_file(path: str, content: str, host: str | None = None, expected_sha256: str | None = None) -> dict:
    """Append remotely with a cooperative lock and atomic replacement."""
    executor = await require_connection(host)
    return await remote_edit(executor, {"op": "append", "path": path, "content": content, "expected_sha256": expected_sha256})


@mcp.tool()
async def ssh_insert_lines(path: str, after_line: int, content: str, host: str | None = None,
                           expected_sha256: str | None = None) -> dict:
    """Insert after a 1-based line, or 0 to prepend. Use expected_sha256 to guard line positions."""
    if after_line < 0:
        raise ValueError("after_line must be nonnegative")
    executor = await require_connection(host)
    return await remote_edit(executor, {"op": "lines", "path": path, "start": after_line, "end": after_line,
        "content": content, "expected_sha256": expected_sha256})


@mcp.tool()
async def ssh_replace_lines(path: str, start_line: int, end_line: int, content: str = "", host: str | None = None,
                            expected_sha256: str | None = None, expected_old: str | None = None) -> dict:
    """Replace inclusive 1-based lines, optionally checking the hash and exact old range."""
    if start_line < 1 or end_line < start_line:
        raise ValueError("Invalid line range")
    payload = {"op": "lines", "path": path, "start": start_line-1, "end": end_line,
               "content": content, "expected_sha256": expected_sha256}
    if expected_old is not None:
        payload["expected_old"] = expected_old
    executor = await require_connection(host)
    return await remote_edit(executor, payload)
