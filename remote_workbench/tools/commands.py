"""Bounded command execution and detached jobs with incremental output."""
from __future__ import annotations
import asyncio
import inspect
import json
import math
import re
import time
import uuid
from remote_workbench import audit
from remote_workbench.server import mcp, require_connection, get_executor
from remote_workbench.remote_client import edit
from remote_workbench import job_collector
from remote_workbench.compute_review import check_compute_review
from remote_workbench.call_arguments import command_text, validate_call_arguments
from remote_workbench.command_spec import Command
from remote_workbench.remote_program import program_command

_SAFE_JOB_ID = re.compile(r"^[a-zA-Z0-9_-]{1,80}$")


@mcp.tool()
async def ssh_run(command: Command, cwd: str | None = None, timeout: int | None = None,
                  host: str | None = None, max_bytes: int = 16384, trace: bool = False,
                  compute_review: dict | None = None, stdin: str | None = None) -> dict:
    """Execute using the remote default shell's interactive login configuration.

    Return head/tail previews bounded by a shared stdout+stderr byte budget.
    A remote supervisor enforces the deadline and cleans its workload scope.
    Inspect remote_task.cleanup_state; transport loss can leave state unknown.
    trace=True saves expanded shell command steps in stderr, including arguments.
    Substantial computation requires explicit user confirmation of command,
    concurrency and environment; record it in compute_review. No OS PID cap.
    """
    validate_call_arguments("exec_command", {"command": command, "cwd": cwd, "timeout": timeout,
                                           "max_bytes": max_bytes, "stdin": stdin})
    display = command_text(command)
    config = get_executor(host).config
    checked_commands = command['commands'] if isinstance(command, dict) else [command]
    for item in checked_commands:
        pending = check_compute_review(command_text(item), cwd or config.default_cwd, config.ssh_target,
                                       compute_review, config.compute_review_threshold)
        if pending:
            return pending
    executor = await require_connection(host)
    input_options = {} if stdin is None else {'input_data':stdin.encode('utf-8')}
    if isinstance(command, dict):
        result = await executor.run('argv batch', cwd=cwd, timeout=timeout, output_budget=max_bytes,
                                    trace=trace, display_command=display, batch=True,
                                    input_data=json.dumps(command, ensure_ascii=False).encode('utf-8'))
    elif isinstance(command, list):
        result = await executor.run(program_command(command), cwd=cwd, timeout=timeout,
                                    output_budget=max_bytes, trace=trace, display_command=display,
                                    **input_options)
    else:
        result = await executor.run(command, cwd=cwd, timeout=timeout, output_budget=max_bytes, trace=trace,
                                    **input_options)
    value = {"stdout": result.stdout, "stderr": result.stderr, "exit_code": result.exit_code,
            "timed_out": result.timed_out, "truncated": result.truncated,
            "output_id": result.output_id, "execution_state": result.execution_state,
            "remote_task": result.remote_task}
    if isinstance(command, dict):
        value['steps'] = (result.remote_task or {}).get('batch', [])
        value['batch_state'] = (result.remote_task or {}).get('batch_state', 'unknown')
        value['remote_task'] = {k:v for k,v in (result.remote_task or {}).items() if k not in {'batch', 'batch_state'}}
    return value


@mcp.tool()
async def ssh_run_background(command: str, cwd: str | None = None, job_id: str = "", host: str | None = None,
                             timeout: int | None = None, compute_review: dict | None = None,
                             idempotency_key: str | None = None, wait_seconds: float = 0,
                             max_bytes: int = 8192) -> dict:
    """Start a supervised detached job with a finite remote deadline.

    Job directories are private and exclusive. Poll using returned byte cursors
    to avoid repeatedly receiving the same output. In session workspaces, an
    idempotency_key protects retries after an ambiguous submission; without a
    key, cross-call retries are not guaranteed to deduplicate.
    Optionally wait up to 30 seconds for locally collected output or completion;
    reaching that wait deadline does not stop or time out the remote job.
    Session workspaces consume idempotency_key locally before calling this tool;
    fixed-workspace mode cannot safely deduplicate ambiguous submissions.
    """
    if isinstance(wait_seconds, bool) or not isinstance(wait_seconds, (int, float)) or \
            not math.isfinite(wait_seconds) or not 0 <= wait_seconds <= 30:
        raise ValueError("wait_seconds must be between 0 and 30")
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or not 256 <= max_bytes <= 65536:
        raise ValueError("max_bytes must be between 256 and 65536")
    if idempotency_key is not None:
        return {"error": "idempotency_key_requires_session_workspace", "executed": False,
                "message": "Idempotency keys require the session workspace registry."}
    job_id = job_id or uuid.uuid4().hex
    if not _SAFE_JOB_ID.fullmatch(job_id):
        return {"error": "invalid_job_id"}
    config = get_executor(host).config
    pending = check_compute_review(command, cwd or config.default_cwd, config.ssh_target,
                                   compute_review, config.compute_review_threshold)
    if pending:
        return pending
    executor = await require_connection(host)
    if timeout is not None and timeout <= 0:
        raise ValueError("timeout must be positive")
    from remote_workbench.resource_claims import mark_job_submission
    mark_job_submission()
    launched = await edit(
        executor, {"op": "job_start", "job_id": job_id, "command": command, "timeout": timeout},
        cwd=cwd or executor.config.default_cwd, max_bytes=max_bytes)
    if wait_seconds > 0 and launched.get("job_id") == job_id and launched.get("job_output_id"):
        waited = await ssh_job_wait(job_id, host=host, wait_seconds=wait_seconds,
                                    stdout_cursor=0, stderr_cursor=0, max_bytes=max_bytes)
        # The launch response contains job setup/provenance fields (including
        # launch_output_id); merge the local wait snapshot without dropping them.
        return {**launched, **waited}
    return launched


@mcp.tool()
async def ssh_stop_job(job_id: str, host: str | None = None) -> dict:
    """Request TERM -> KILL of a supervised job, even with execution disabled.

    Refuses foreign-node, reused-PID and legacy handles. Acknowledgement is
    separate from a request; inspect cleanup_state and tree_confirmed.
    """
    if not _SAFE_JOB_ID.fullmatch(job_id):
        return {"error": "invalid_job_id"}
    executor = get_executor(host)
    origin = job_collector.current_origin(executor, job_id)
    if origin is not None:
        origin = job_collector.origin_for_control(executor, job_id, origin)
        if origin.get("error"):
            return origin
    return await executor.cancel_task(job_id, origin=origin)


@mcp.tool()
async def ssh_job_status(job_id: str, tail_lines: int = 50, host: str | None = None,
                         stdout_cursor: int = 0, stderr_cursor: int = 0, max_bytes: int = 8192) -> dict:
    """Get job state and incremental output. Reuse next_stdout_cursor/next_stderr_cursor.

    tail_lines is a compatibility display cap; cursors count bytes in the remote
    logs. Per-stream pages are limited to max_bytes/2 before the display line cap.
    running=None and execution_state='unknown' mean node/process identity could
    not be verified. Missing completion is never proof that a job has stopped.
    """
    if not _SAFE_JOB_ID.fullmatch(job_id):
        return {"error": "invalid_job_id"}
    if min(stdout_cursor, stderr_cursor) < 0 or not 1 <= tail_lines <= 1000 or not 256 <= max_bytes <= 65536:
        raise ValueError("Invalid cursor/tail_lines/max_bytes")
    executor = get_executor(host)
    ensured = await _ensure_job_collection(executor, job_id)
    if (job_collector.current_origin(executor, job_id) is not None and
            isinstance(ensured, dict) and ensured.get("error")):
        return ensured
    local = _local_job_snapshot(executor, job_id)
    if (not local or not local.get("job_output_id")) and isinstance(ensured, dict):
        local = ensured
    if local and local.get("job_output_id"):
        output_id = local["job_output_id"]
        sizes = _local_job_sizes(output_id)
        if stdout_cursor > sizes["stdout"] or stderr_cursor > sizes["stderr"]:
            raise ValueError("output cursor exceeds locally collected bytes")
        out = audit.read_output(output_id, "stdout", stdout_cursor, max_bytes // 2, tail_lines) \
            if (audit.directory(output_id) / "stdout").exists() else {
                "content": "", "next_cursor": stdout_cursor, "truncated": False}
        err = audit.read_output(output_id, "stderr", stderr_cursor, max_bytes // 2, tail_lines) \
            if (audit.directory(output_id) / "stderr").exists() else {
                "content": "", "next_cursor": stderr_cursor, "truncated": False}
        return {
            "job_id": job_id,
            "running": local.get("running"),
            "exit_code": local.get("exit_code"),
            "execution_state": local.get("execution_state", "unknown"),
            "sync_state": local.get("sync_state", "unknown"),
            "remote_task": local.get("remote_task"),
            "job_output_id": output_id,
            "output_id": output_id,
            "stdout_tail": out["content"],
            "stderr_tail": err["content"],
            "next_stdout_cursor": out["next_cursor"],
            "next_stderr_cursor": err["next_cursor"],
            "stdout_has_more": out["truncated"],
            "stderr_has_more": err["truncated"],
        }
    executor = await require_connection(host)
    return await edit(executor, {"op": "job_status", "job_id": job_id, "stdout_cursor": stdout_cursor,
        "stderr_cursor": stderr_cursor, "budget": max_bytes // 2, "tail_lines": tail_lines}, max_bytes=max_bytes)


def _local_job_snapshot(executor, job_id):
    """Read the stable local handle installed by the background log collector."""
    lookup = getattr(executor, "job_local_info", None)
    if lookup is None:
        return None
    value = lookup(job_id)
    if inspect.isawaitable(value):
        raise TypeError("job_local_info must be a synchronous local lookup")
    return value if isinstance(value, dict) else None


async def _ensure_job_collection(executor, job_id):
    """Let the local collector reconnect or recover its manifest if supported."""
    ensure = getattr(executor, "ensure_job_collection", None)
    if ensure is None:
        return None
    origin = job_collector.current_origin(executor, job_id)
    if origin is None:
        value = ensure(job_id)
    else:
        try:
            parameters = inspect.signature(ensure).parameters
        except (TypeError, ValueError):
            parameters = {}
        value = ensure(job_id, origin=origin) if "origin" in parameters else ensure(job_id)
    return await value if inspect.isawaitable(value) else value


def _local_job_sizes(output_id):
    directory = audit.directory(output_id)
    sizes = {}
    for stream in ("stdout", "stderr"):
        path = directory / stream
        try:
            sizes[stream] = path.stat().st_size
        except FileNotFoundError:
            sizes[stream] = 0
    return sizes


def _job_is_terminal(info):
    if info.get("sync_state") in {"complete", "disconnected"}:
        return True
    if info.get("sync_state") in {"syncing", "streaming"}:
        return False
    return (info.get("running") is False or
            info.get("execution_state") in {"finished", "failed", "cancelled", "timed_out"})


@mcp.tool()
async def ssh_job_wait(job_id: str, host: str | None = None, wait_seconds: float = 10.0,
                       stdout_cursor: int = 0, stderr_cursor: int = 0, max_bytes: int = 8192) -> dict:
    """Wait once for local background-log progress, completion or a bounded wait deadline.

    The collector owns any resumed read-only log channel; this wait does not
    repeatedly poll remote job status. Pass returned byte cursors on the next
    call to avoid duplicate logs.
    wait_expired only means this wait ended; inspect running/remote_task for the
    actual job deadline and state.
    """
    if not _SAFE_JOB_ID.fullmatch(job_id):
        return {"error": "invalid_job_id"}
    if isinstance(wait_seconds, bool) or not isinstance(wait_seconds, (int, float)) or \
            not math.isfinite(wait_seconds) or not 0 <= wait_seconds <= 60:
        raise ValueError("wait_seconds must be between 0 and 60")
    if isinstance(stdout_cursor, bool) or isinstance(stderr_cursor, bool) or \
            not isinstance(stdout_cursor, int) or not isinstance(stderr_cursor, int) or \
            min(stdout_cursor, stderr_cursor) < 0:
        raise ValueError("output cursors must be nonnegative integers")
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or not 256 <= max_bytes <= 65536:
        raise ValueError("max_bytes must be between 256 and 65536")

    executor = get_executor(host)
    ensured = await _ensure_job_collection(executor, job_id)
    info = _local_job_snapshot(executor, job_id)
    if (not info or not info.get("job_output_id")) and isinstance(ensured, dict):
        info = ensured
    if (job_collector.current_origin(executor, job_id) is not None and
            isinstance(ensured, dict) and ensured.get("error")):
        return ensured
    if not info or not info.get("job_output_id"):
        return {"job_id": job_id, "error": "local_job_unavailable", "running": None,
                "execution_state": "unknown", "sync_state": "unknown",
                "message": (ensured.get("sync_error") or ensured.get("message")
                            if isinstance(ensured, dict) else None) or
                           "No locally collected output is available for this job."}
    output_id = info["job_output_id"]
    sizes = _local_job_sizes(output_id)
    if stdout_cursor > sizes["stdout"] or stderr_cursor > sizes["stderr"]:
        raise ValueError("output cursor exceeds locally collected bytes")

    def has_unread_output(current_sizes):
        return current_sizes["stdout"] > stdout_cursor or current_sizes["stderr"] > stderr_cursor

    reason = None
    if has_unread_output(sizes):
        reason = "output"
    elif _job_is_terminal(info):
        reason = "state_changed"

    deadline = time.monotonic() + float(wait_seconds)
    while reason is None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            reason = "wait_deadline"
            break
        await asyncio.sleep(min(0.1, remaining))
        current = _local_job_snapshot(executor, job_id)
        if not current or current.get("job_output_id") != output_id:
            info = {"job_output_id": output_id, "running": None,
                    "execution_state": "unknown", "sync_state": "disconnected"}
            reason = "state_changed"
            break
        info = current
        sizes = _local_job_sizes(output_id)
        if has_unread_output(sizes):
            reason = "output"
        elif _job_is_terminal(info):
            reason = "state_changed"

    out = audit.read_output(output_id, "stdout", stdout_cursor, max_bytes // 2) \
        if (audit.directory(output_id) / "stdout").exists() else {
            "content": "", "next_cursor": stdout_cursor, "truncated": False, "total_bytes": 0}
    err = audit.read_output(output_id, "stderr", stderr_cursor, max_bytes // 2) \
        if (audit.directory(output_id) / "stderr").exists() else {
            "content": "", "next_cursor": stderr_cursor, "truncated": False, "total_bytes": 0}
    return {
        "job_id": job_id,
        "job_output_id": output_id,
        "running": info.get("running"),
        "exit_code": info.get("exit_code"),
        "execution_state": info.get("execution_state", "unknown"),
        "sync_state": info.get("sync_state", "unknown"),
        "remote_task": info.get("remote_task"),
        "stdout_tail": out["content"],
        "stderr_tail": err["content"],
        "next_stdout_cursor": out["next_cursor"],
        "next_stderr_cursor": err["next_cursor"],
        "stdout_has_more": out["truncated"],
        "stderr_has_more": err["truncated"],
        "wait_expired": reason == "wait_deadline",
        "wait_reason": reason,
    }
