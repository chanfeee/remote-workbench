"""Read locally stored command records without executing remote commands."""
import json
from remote_workbench import audit
from remote_workbench.log_search import search_saved_output
from remote_workbench.tool_runtime import audited_operation


@audited_operation
async def ssh_read_output(output_id: str, stream: str = "stdout", cursor: int = 0, max_bytes: int = 8192) -> dict:
    """Read bounded stdout/stderr/diff/stdin or separate startup_stdout/startup_stderr."""
    return audit.read_output(output_id, stream, cursor, max_bytes)


@audited_operation
async def ssh_search_output(output_id: str, query: str, stream: str = "stdout", cursor: int = 0,
                            max_scan_bytes: int = 65536, context_bytes: int = 96,
                            max_matches: int = 20, max_bytes: int = 8192) -> dict:
    """Search one page of a locally saved output spool; returns byte offsets and bounded context."""
    return search_saved_output(output_id, query, stream, cursor, max_scan_bytes,
                               context_bytes, max_matches, max_bytes)


@audited_operation
async def ssh_audit_list(limit: int = 30) -> dict:
    """List recent operation metadata; retrieve a record only when needed."""
    return {"records": audit.recent(limit)}


@audited_operation
async def ssh_audit_get(operation_id: str) -> dict:
    """Get command, wrapper, arguments, execution status and child command IDs."""
    return json.loads((audit.directory(operation_id) / "record.json").read_text())
