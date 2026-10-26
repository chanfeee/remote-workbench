from remote_workbench.server import mcp, require_connection
from remote_workbench.remote_client import edit
from remote_workbench.call_arguments import patch_text
from remote_workbench.edit_spec import FileEdit, edits_patch, prepared_edits, validate_prepared_edits


@mcp.tool()
async def ssh_apply_patch(patch: str | list[str] | None = None, cwd: str = ".", host: str | None = None, check_only: bool = False,
                          edits: list[FileEdit] | None = None, expected_files: dict[str, str] | None = None,
                          expected_repository: str | None = None, prepared_contents: dict[str, str] | None = None) -> dict:
    """Check/apply a context patch (*** Begin Patch) or unified Git patch. Worktree only.

    Uses git apply with default path protections and no --reject/--index. Check
    and apply are serialized against other calls to this tool, not external editors.
    Full submitted patch is available as the command record's stdin JSON.
    """
    if edits is not None:
        if patch is not None:
            raise ValueError("Pass edits or patch, not both")
        if prepared_edits(edits):
            validate_prepared_edits(edits)
        else:
            patch = edits_patch(edits)
            edits = None
    if edits is None and (patch != '' or not expected_files):
        patch = patch_text(patch)
    executor = await require_connection(host)
    if edits is not None:
        from remote_workbench.prepared_edit import materialize
        prepared = materialize(executor, edits)
        patch, expected_files, expected_repository = (prepared[k] for k in ('patch', 'expected_files', 'expected_repository'))
        prepared_contents = prepared['prepared_contents']
    return await edit(executor, {"op": "apply_patch", "patch": patch, "check_only": check_only,
                                 "expected_files": expected_files, "expected_repository": expected_repository,
                                 "prepared_contents": prepared_contents}, cwd=cwd)
