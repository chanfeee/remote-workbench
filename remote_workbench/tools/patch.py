from remote_workbench.server import mcp, require_connection
from remote_workbench.remote_client import edit


@mcp.tool()
async def ssh_apply_patch(patch: str, cwd: str, host: str | None = None, check_only: bool = False) -> dict:
    """Check/apply a context patch (*** Begin Patch) or unified Git patch. Worktree only.

    Uses git apply with default path protections and no --reject/--index. Check
    and apply are serialized against other calls to this tool, not external editors.
    Full submitted patch is available as the command record's stdin JSON.
    """
    if not patch.strip() or len(patch.encode()) > 10485760:
        raise ValueError("patch must contain 1..10485760 UTF-8 bytes")
    executor = await require_connection(host)
    return await edit(executor, {"op": "apply_patch", "patch": patch, "check_only": check_only}, cwd=cwd)
