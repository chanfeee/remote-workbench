"""Legacy SSH MCP adapter; importing operations never constructs this server."""
from mcp.server.fastmcp import FastMCP

from remote_workbench.compute_review import REVIEW_INSTRUCTIONS
# Preserve existing Python imports while the implementation lives below MCP.
from remote_workbench.tool_runtime import get_executor, require_connection, workspace_executor
from remote_workbench.tools import audit, commands, connection, files, git, patch, search


mcp = FastMCP(
    "remote-workbench-ssh",
    instructions=(
        "Use explicit host and an existing ControlMaster. Inspect, edit and test on the remote. "
        "Use ssh_patch_file for unique/batch replacements and ssh_apply_patch for multi-file patches. "
        "ssh_git_diff defaults to summary; mode='patch' pages a saved snapshot. Use output_id "
        "with ssh_read_output for full saved output. Verify edits with diff/status and relevant checks. "
        "If disconnected, ask the user to reconnect."
        + REVIEW_INSTRUCTIONS
    ),
)

for operation in (
    connection.ssh_check_connection,
    commands.ssh_run, commands.ssh_run_background, commands.ssh_stop_job,
    commands.ssh_job_status, commands.ssh_job_wait,
    files.ssh_read_file, files.ssh_write_file, files.ssh_list_dir, files.ssh_file_info,
    files.ssh_patch_file, files.ssh_mkdir, files.ssh_move, files.ssh_delete,
    files.ssh_fetch_image, files.ssh_download, files.ssh_append_file,
    files.ssh_insert_lines, files.ssh_replace_lines,
    search.ssh_grep, search.ssh_find_files,
    git.ssh_git_status, git.ssh_git_diff, git.ssh_git_log,
    audit.ssh_read_output, audit.ssh_search_output, audit.ssh_audit_list, audit.ssh_audit_get,
    patch.ssh_apply_patch,
):
    mcp.add_tool(operation)
