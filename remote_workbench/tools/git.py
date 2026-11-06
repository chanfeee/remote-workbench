"""Git operation tools on the remote server."""

from __future__ import annotations

from remote_workbench import audit

from remote_workbench.tool_runtime import audited_operation, require_connection


@audited_operation
async def ssh_git_status(
    cwd: str | None = None,
    host: str | None = None,
) -> dict:
    """Get git status of a repository on the remote server.

    Returns the current branch, modified files, staged files, and untracked files.

    Args:
        cwd: Path to the git repository. Defaults to host's default_cwd.
        host: SSH host alias from config. Uses default if omitted.
    """
    executor = await require_connection(host)

    result = await executor.run_program(["git", "status", "--porcelain=v1", "-z", "-b"], cwd=cwd)
    if result.exit_code != 0:
        return {"error": "git_failed", "message": result.stderr.strip()}

    if result.truncated:
        return {"error": "status_output_exceeds_budget", "output_id": result.output_id,
                "message": "Read saved stdout; do not infer a clean repository from partial status"}
    lines = iter(result.stdout.split("\0"))
    branch = ""
    modified = []
    staged = []
    untracked = []

    for line in lines:
        if line.startswith("## "):
            branch = line[3:].split("...")[0]
            continue
        if len(line) < 2:
            continue
        index_status = line[0]
        work_status = line[1]
        filepath = line[3:]
        if "R" in line[:2] or "C" in line[:2]:
            next(lines, "")  # Original name follows destination in porcelain -z.

        if index_status == "?":
            untracked.append(filepath)
        else:
            if index_status not in (" ", "?"):
                staged.append(f"{index_status} {filepath}")
            if work_status != " ":
                modified.append(f"{work_status} {filepath}")

    return {
        "branch": branch,
        "staged": staged,
        "modified": modified,
        "untracked": untracked,
        "clean": not (staged or modified or untracked),
        "output_id": result.output_id,
    }


def diff_page(output_id: str, cursor: int, max_bytes: int) -> dict:
    """Page at hunk/file boundaries; oversized hunks use explicit byte retrieval."""
    path = audit.directory(output_id) / "stdout"
    size = path.stat().st_size
    if cursor < 0 or cursor > size:
        raise ValueError("Invalid diff cursor")
    chunks = []
    used = 0
    next_cursor = cursor
    with path.open("rb") as source:
        source.seek(cursor)
        segment_start = cursor
        segment = bytearray()
        oversized = False
        def emit(end):
            nonlocal used, next_cursor
            if oversized:
                if chunks:
                    return {"diff": b"".join(chunks).decode("utf-8", "replace"), "output_id": output_id,
                            "next_cursor": segment_start, "truncated": True, "total_bytes": size}
                return {"error": "hunk_exceeds_budget", "output_id": output_id,
                        "byte_cursor": segment_start, "hunk_end": end,
                        "message": "Use ssh_read_output for this hunk, or increase max_bytes"}
            if used + len(segment) > max_bytes:
                return {"diff": b"".join(chunks).decode("utf-8", "replace"), "output_id": output_id,
                        "next_cursor": segment_start, "truncated": True, "total_bytes": size}
            chunks.append(bytes(segment))
            used += len(segment)
            next_cursor = end
            return None
        while True:
            position = source.tell()
            line = source.readline(max_bytes + 1)
            if not line:
                stop = emit(position)
                if stop:
                    return stop
                break
            boundary = line.startswith((b"@@ ", b"diff --git "))
            if boundary and position > segment_start:
                stop = emit(position)
                if stop:
                    return stop
                segment_start = position
                segment = bytearray()
                oversized = False
            if len(segment) + len(line) <= max_bytes:
                segment.extend(line)
            else:
                oversized = True
    return {"diff": b"".join(chunks).decode("utf-8", "replace"), "output_id": output_id,
            "next_cursor": next_cursor, "truncated": next_cursor < size, "total_bytes": size, "empty": size == 0}


@audited_operation
async def ssh_git_diff(cwd: str | None = None, ref: str = "", path: str = "", staged: bool = False,
                       host: str | None = None, mode: str = "summary", paths: list[str] | None = None,
                       context: int = 3, max_bytes: int = 8192, output_id: str = "", cursor: int = 0) -> dict:
    """Default: compact numstat. mode='patch': bounded hunks with a stable snapshot.

    To continue an existing patch snapshot pass output_id and next_cursor; no
    remote command is repeated. Untracked file contents are not part of git diff.
    """
    if mode not in {"summary", "patch"} or not 0 <= context <= 50 or not 256 <= max_bytes <= 65536:
        raise ValueError("Invalid mode/context/max_bytes")
    if output_id:
        return diff_page(output_id, cursor, max_bytes)
    if cursor:
        raise ValueError("cursor requires output_id")
    if ref.startswith("-"):
        raise ValueError("ref cannot begin with '-'")
    executor = await require_connection(host)
    args = ["git", "--no-pager", "--literal-pathspecs", "diff", "--no-ext-diff", "--no-textconv", "--no-color"]
    args += ["--numstat"] if mode == "summary" else [f"--unified={context}"]
    if staged:
        args.append("--cached")
    if ref:
        args.append(ref)
    args += ["--", *(paths or ([path] if path else []))]
    result = await executor.run_program(args, cwd=cwd, output_budget=max_bytes)
    if result.exit_code != 0:
        return {"error": "git_failed", "message": result.stderr, "output_id": result.output_id}
    if mode == "patch":
        return diff_page(result.output_id, 0, max_bytes)
    return {"summary": result.stdout, "output_id": result.output_id, "truncated": result.truncated,
            "empty": not result.stdout.strip(), "untracked_included": False}


@audited_operation
async def ssh_git_log(
    cwd: str | None = None,
    count: int = 10,
    path: str = "",
    oneline: bool = False,
    host: str | None = None,
) -> dict:
    """Show git log on the remote server.

    Args:
        cwd: Path to the git repository. Defaults to host's default_cwd.
        count: Number of commits to show.
        path: Limit log to a specific file or directory.
        oneline: If true, show compact one-line format.
        host: SSH host alias from config. Uses default if omitted.
    """
    executor = await require_connection(host)
    count = int(count)

    if oneline:
        fmt = "--oneline"
    else:
        fmt = "--format=%H|%an|%ai|%s"

    cmd_parts = ["git", "log", fmt, f"-n{count}"]
    if path:
        cmd_parts.extend(["--", path])

    result = await executor.run_program(cmd_parts, cwd=cwd)

    if result.exit_code != 0:
        return {"error": "git_failed", "message": result.stderr.strip()}

    if oneline:
        return {"log": result.stdout.strip(), "count": count}

    commits = []
    for line in result.stdout.strip().split("\n"):
        if not line:
            continue
        parts = line.split("|", 3)
        if len(parts) >= 4:
            commits.append({
                "hash": parts[0],
                "author": parts[1],
                "date": parts[2],
                "message": parts[3],
            })

    return {"commits": commits, "count": len(commits)}
