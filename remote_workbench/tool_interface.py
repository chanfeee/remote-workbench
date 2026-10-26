"""Model-facing projections; complete command/output records stay local.

Only known redundant metadata is omitted. Unknown fields, failures, warnings,
unknown execution state and cleanup uncertainty remain visible to the agent.
"""
import re

from remote_workbench import audit
from remote_workbench.presentation import is_test_command, summarize_output
from remote_workbench.call_arguments import command_text


TOOL_NAMES = frozenset({
    "read_file", "workspace_switch", "workspace_use_local", "exec_command",
    "apply_patch", "read_output", "fetch_image",
})

CALL_SOURCE_INSTRUCTIONS = (
    'Use workspace_switch(command=...,read_files=[paths],edit_files=[paths]) for binding, initial checks and selected reads in one call. '
    'exec_command uses command=shell_text or command=[program,args]. '
    'command={commands:[[program,args],...]} batches sequential checks in one SSH call. '
    'Read files with read_file(for_edit=true); apply_patch uses edits=[{edit_id,new:[source lines]}], optionally start_line/end_line. '
    'Do not handwrite patch markers or use native staging edits in the normal workflow. '
    'apply_patch(command=...) runs explicit checks after successful edits; returned edits contain next edit_ids, so no reread is needed for already-known content. '
    'Print MCP text blocks once, not the serialized envelope or ALL_TOOLS catalog. '
)

INSTRUCTIONS = (
    'Local control; remote execution only for a host/cwd assigned in this chat. New chats start local. '
    'workspace_switch binds the actual directory; never choose a managed project copy for normal work. '
    'cwd, code/environment paths and result paths may be independent; pass them explicitly. Native tools stay local. No extra probe, catalog or metadata gate. '
    'Use a fresh account shell; activate environments per call. No remote Codex CLI. '
    'On identity/SSH failure stop; never reauthenticate or run requested remote work locally. '
    + CALL_SOURCE_INSTRUCTIONS +
    'Jobs/advanced tools use workspace_tools; old jobs retain their origin. Full output/diffs stay local; read_output pages them. '
    'Include the supplied **Remote Workbench 操作记录** link in replies. fetch_image returns chat references, never base64. '
    'Before heavy/parallel compute, scheduler submission or runtime/spawn tests: show command, concurrency and environment, wait for explicit user confirmation, then pass compute_review. Not an OS sandbox.'
)

_PROGRESS = re.compile(
    r"(?:\s*test\S* .* \.\.\.(?:[ \t]+(?:ok|skipped .+|expected failure|unexpected success))?[ \t]*|"
    r"UNEXPECTED SUCCESS: .+|[.sx]+)"
)


def _test_summary(result, command):
    if isinstance(command, dict):
        return None  # Batch step exits are execution facts, not one test report.
    if isinstance(command, list):
        command = command_text(command)
    if (not is_test_command(command) or result.get("error") or result.get("timed_out")
            or result.get("execution_state") != "finished"):
        return None
    output_id = result.get("output_id", "")
    streams = {}
    for stream in ("stdout", "stderr"):
        text = result.get(stream, "")
        if result.get("truncated"):
            # Summarize only complete, bounded local spools, never a partial page.
            if not re.fullmatch(r"[a-f0-9]{32}", output_id):
                return None
            try:
                path = audit.directory(output_id) / stream
                if path.stat().st_size > 1048576:
                    return None
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeError):
                return None
        streams[stream] = text
    summaries = []
    for stream, text in streams.items():
        if not text.strip():
            continue
        summary = summarize_output(text, result, stream, complete=True)
        if not summary or summary["state"] not in {"success", "completed", "failed"}:
            return None
        # Keep warnings, prints, fixture diagnostics and arbitrary wrapper output.
        # A recognized footer alone does not justify hiding everything before it.
        for line in text.splitlines():
            if (line.strip() and not _PROGRESS.fullmatch(line)
                    and not re.fullmatch(r"[-=]+", line)
                    and not re.fullmatch(r"Ran \d+ tests? in [0-9.]+s", line)
                    and not re.fullmatch(r"(?:OK|FAILED)(?: \([^\r\n]*\))?", line)):
                return None
        summaries.append(summary)
    if len(summaries) != 1:
        return None
    summary = summaries[0]
    value = {
        "status": "passed" if summary["state"] == "success" else summary["state"],
        "tests": summary["passed"],
        "tests_run": summary["tests_run"],
        "reported_skips": summary["reported_skips"],
        "executed": summary["executed"],
        "passed": summary["passed"],
        "report_status": summary["report_status"],
        "process_status": summary["process_status"],
        # These aliases remain useful to existing model-facing consumers.
        "total": summary["tests_run"],
        "skipped": summary["reported_skips"],
    }
    for key in ("expected_failures", "unexpected_successes", "reported_failures", "reported_errors"):
        if key in summary:
            value[key] = summary[key]
    return value


def present_result(name, result, arguments=None):
    value = dict(result)
    if name == "audit_get":
        # An explicit record read must retain its own original revision/origin.
        return value
    if name == "exec_command":
        summary = _test_summary(value, (arguments or {}).get("command", ""))
        if summary is not None:
            value["test_summary"] = summary
            if summary["status"] != "failed":
                value.pop("stdout", None)
                value.pop("stderr", None)
    if isinstance(value.get('command_result'), dict):
        value['command_result'] = present_result('exec_command', value['command_result'], arguments)
    if name in {"workspace_switch", "workspace_use_local"}:
        redundant = {"thread_id", "created_at", "updated_at", "previous"}
        value = {key: item for key, item in value.items() if key not in redundant}
        if isinstance(value.get("initial"), dict):
            value["initial"] = present_result("exec_command", value["initial"], arguments)
    elif name not in {"workspace_info", "workspace_resume", "workspace_history"}:
        value.pop("workspace_id", None)
        value.pop("revision", None)
    if value.get("output_id") and value.get("operation_id") == value["output_id"]:
        value.pop("operation_id", None)
    task = value.get("remote_task")
    if (isinstance(task, dict) and task.get("state") == "finished" and task.get("reason") == "finished"
            and value.get("exit_code") == 0 and value.get("execution_state") == "finished"
            and not value.get("error") and not value.get("timed_out")
            and task.get("cleanup_state") in {"not_started", "process_group_signalled"}):
        # Origin/recovery details stay in the command record. Preserve explicit
        # cleanup limits (including tree_confirmed=False), and unknown fields.
        origin_fields = {"node", "boot_id", "version", "pid", "start_token", "task_id", "task_dir",
                         "backend", "timeout_seconds", "workload_pid", "pgid", "state", "reason", "exit_code"}
        value["remote_task"] = {key: item for key, item in task.items() if key not in origin_fields}
    for key in ("stdout", "stderr"):
        if value.get(key) == "":
            value.pop(key)
    for key in ("timed_out", "truncated", "diff_truncated"):
        if value.get(key) is False:
            value.pop(key)
    return value
