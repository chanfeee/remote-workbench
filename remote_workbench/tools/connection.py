"""Connection management tools."""

from __future__ import annotations

from remote_workbench.tool_runtime import audited_operation, get_executor


@audited_operation
async def ssh_check_connection(host: str | None = None) -> dict:
    """Check if the SSH ControlMaster connection is alive.

    Returns connection status and diagnostic info. If disconnected,
    provides the exact command the user needs to run to re-authenticate.

    Args:
        host: SSH host alias from config. Uses default if omitted.
    """
    executor = get_executor(host)
    connected, message = await executor.check_connection()
    return {
        "connected": connected,
        "host": executor.config.ssh_host,
        "control_path": executor.config.resolved_control_path,
        "message": message,
    }
