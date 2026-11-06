"""Search tools — grep and find on the remote server."""

from __future__ import annotations

from remote_workbench.tool_runtime import audited_operation, require_connection


@audited_operation
async def ssh_grep(
    pattern: str,
    path: str = ".",
    glob: str = "",
    max_results: int = 100,
    context_lines: int = 0,
    case_insensitive: bool = False,
    host: str | None = None,
) -> dict:
    """Search for a pattern in files on the remote server using grep.

    Returns matching lines with file paths and line numbers.

    Args:
        pattern: Regular expression pattern to search for.
        path: Directory or file to search in.
        glob: Filter files by glob pattern (e.g., "*.py", "*.cpp").
        max_results: Maximum number of matching lines to return.
        context_lines: Number of context lines to show before and after each match.
        case_insensitive: If true, ignore case when matching.
        host: SSH host alias from config. Uses default if omitted.
    """
    executor = await require_connection(host)
    context_lines = int(context_lines)
    max_results = int(max_results)
    if not 1 <= max_results <= 2000 or not 0 <= context_lines <= 100:
        raise ValueError("Invalid max_results/context_lines")
    if not path:
        raise ValueError("path must not be empty")

    # GNU grep otherwise omits the filename when path names one file.
    args = ["grep", "-rnH"]
    if case_insensitive:
        args.append("-i")
    if context_lines > 0:
        args.extend(["-C", str(context_lines)])
    if glob:
        args.extend(["--include", glob])
    args.extend(["--", pattern, path])

    result = await executor.run_program(
        args, max_lines=max_results + 1, path_indices=[len(args) - 1]
    )

    if result.exit_code == 1 and not result.stdout:
        return {"matches": [], "count": 0, "truncated": False, "message": "No matches found."}

    if result.exit_code not in (0, 1):
        return {"error": "grep_failed", "message": result.stderr.strip(), "output_id": result.output_id}

    lines = result.stdout.split("\n") if result.stdout else []
    if lines and lines[-1] == "":
        lines.pop()
    truncated = len(lines) > max_results or result.truncated
    lines = lines[:max_results]

    return {
        "matches": lines,
        "count": len(lines),
        "truncated": truncated,
        "output_id": result.output_id,
    }


@audited_operation
async def ssh_find_files(
    path: str = ".",
    pattern: str = "*",
    file_type: str = "f",
    max_results: int = 200,
    max_depth: int = 10,
    host: str | None = None,
) -> dict:
    """Find files by name pattern on the remote server.

    Args:
        path: Directory to search in.
        pattern: Filename glob pattern (e.g., "*.py", "test_*").
        file_type: Type filter: "f" for files, "d" for directories, "" for both.
        max_results: Maximum entries to return.
        max_depth: Maximum directory depth to search.
        host: SSH host alias from config. Uses default if omitted.
    """
    executor = await require_connection(host)
    max_depth = int(max_depth)
    max_results = int(max_results)
    if not 1 <= max_results <= 2000 or not 0 <= max_depth <= 100:
        raise ValueError("Invalid max_results/max_depth")
    if not path:
        raise ValueError("path must not be empty")

    # Validate file_type against allowlist to prevent injection
    valid_types = {"f", "d", "l", "b", "c", "p", "s", ""}
    if file_type not in valid_types:
        return {"error": "invalid_type", "message": f"file_type must be one of: {', '.join(sorted(valid_types - {''}))} (or empty)"}

    args = ["find", path, "-maxdepth", str(max_depth)]
    if file_type:
        args.extend(["-type", file_type])
    args.extend(["-name", pattern])

    result = await executor.run_program(args, max_lines=max_results + 1, path_indices=[1])
    if result.exit_code != 0:
        return {"error": "find_failed", "message": result.stderr.strip(), "output_id": result.output_id}

    lines = result.stdout.split("\n") if result.stdout else []
    if lines and lines[-1] == "":
        lines.pop()
    truncated = len(lines) > max_results or result.truncated
    lines = lines[:max_results]

    return {
        "files": lines,
        "count": len(lines),
        "truncated": truncated,
        "output_id": result.output_id,
    }
