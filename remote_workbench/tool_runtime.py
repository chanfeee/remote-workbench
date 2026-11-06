"""Shared operation auditing and executor selection, independent of MCP registration."""
from __future__ import annotations

from contextvars import ContextVar
import functools
import inspect
import time

from remote_workbench import audit
from remote_workbench.auth_boundary import authentication_command, refusal
from remote_workbench.config import get_config
from remote_workbench.ssh import SSHExecutor


_AUDIT_READERS = frozenset({
    "ssh_read_output", "ssh_search_output", "ssh_job_status", "ssh_job_wait",
    "ssh_audit_list", "ssh_audit_get",
})
_executors: dict[str, SSHExecutor] = {}
workspace_executor: ContextVar[tuple[str, SSHExecutor] | None] = ContextVar(
    "remote-workbench_workspace_executor", default=None
)


def authentication_refusal(tool, command, **location):
    """Reject authentication before execution, retaining only a redacted record."""
    if not authentication_command(command):
        return None
    record_id = audit.create(
        kind="tool", tool=tool, arguments={"command": "[authentication arguments omitted]"},
        status="failed", error="authentication_requires_user_terminal", **location,
    )
    return dict(refusal(), operation_id=record_id)


def audited_operation(function):
    """Record an operation without constructing or registering an MCP tool."""
    if function.__name__ in _AUDIT_READERS:
        return function

    @functools.wraps(function)
    async def call(*args, **kwargs):
        bound = inspect.signature(function).bind(*args, **kwargs)
        if function.__name__ in {"ssh_run", "ssh_run_background"}:
            refused = authentication_refusal(function.__name__, bound.arguments.get("command", ""))
            if refused is not None:
                return refused
        record_id = audit.create(
            kind="tool", tool=function.__name__, arguments=dict(bound.arguments),
            host=bound.arguments.get("host"), cwd=bound.arguments.get("cwd"), status="running",
        )
        token = audit.operation_id.set(record_id)
        started = time.monotonic()
        try:
            result = await function(*args, **kwargs)
            audit.update(
                record_id,
                status="failed" if isinstance(result, dict) and result.get("error") else "finished",
                output_id=result.get("output_id") if isinstance(result, dict) else None,
                elapsed_ms=round((time.monotonic() - started) * 1000),
            )
            if isinstance(result, dict):
                result.setdefault("operation_id", record_id)
            return result
        except BaseException as exc:
            audit.update(record_id, status="failed", error=str(exc))
            raise
        finally:
            audit.operation_id.reset(token)

    return call


def get_executor(host: str | None = None) -> SSHExecutor:
    """Use the request's immutable binding, or the legacy per-host cache."""
    binding = workspace_executor.get()
    if binding is not None:
        alias, executor = binding
        if host is not None and host != alias:
            raise ValueError("This workspace is bound to a different host")
        return executor
    config = get_config()
    host_config = config.get_host(host)
    key = host or config.default_host or next(iter(config.hosts))
    if key not in _executors:
        _executors[key] = SSHExecutor(host_config)
    return _executors[key]


async def require_connection(host: str | None = None) -> SSHExecutor:
    executor = get_executor(host)
    connected, message = await executor.check_connection()
    if not connected:
        raise ValueError(message)
    return executor
