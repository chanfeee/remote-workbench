"""Model-facing projections; complete command/output records stay local.

Only known redundant metadata is omitted. Unknown fields, failures, warnings,
unknown execution state and cleanup uncertainty remain visible to the agent.
"""
import posixpath
import re

from remote_workbench import audit
from remote_workbench.presentation import is_test_command, summarize_output
from remote_workbench.call_arguments import command_text


TOOL_NAMES = frozenset({
    "read_file", "workspace_switch", "workspace_use_local", "exec_command", "exec_batch", "inspect_batch", "review_activity",
    "apply_patch", "read_output", "fetch_image",
})

# These operations leave source files, bindings and jobs unchanged. Local
# output caches/audit bookkeeping do not grant permission to mutate the source.
READ_ONLY_TOOL_NAMES = frozenset({"read_file", "read_output", "fetch_image", "inspect_batch", "review_activity"})

CALL_SOURCE_INSTRUCTIONS = (
    'Use schemas; print tool text once. '
    'Full output/diffs stay local; read_output pages them. Advanced tools: workspace_tools.'
)

COMMAND_BATCH_GUIDANCE = (
    'Prefer one {commands:[argv,...]} batch of up to 8 already-known commands in the same cwd. '
    'Submit smaller ready batches immediately. Each batch runs sequentially and returns one native result '
    'with per-step exits. Stop on error by default. Split calls when later arguments depend on earlier output. '
    'Shell text and single argv also work. '
)

# Clients may repeat server instructions in every deferred tool description.
# Keep shared boundaries here; parameter details belong to each tool's schema.
INSTRUCTIONS = (
    'New chats start local. Give host and absolute cwd; reuse bindings. '
    'Native tools stay local; no remote Codex CLI. '
    'Use manual SSH; on identity/SSH failure stop without reauth or local fallback. '
    'Direct heavy/parallel compute and runtime/spawn tests need user confirmation and compute_review. '
    'Scheduler submission uses ordinary approval. '
    'Default to inspect_batch for supported read-only probes, exec_batch for other groups. '
    'Edit and check with apply_patch. '
    'Inline cards show diffs and checks. Flag failures, blockers and decisions separately. '
    'Report major actions. '
    + CALL_SOURCE_INSTRUCTIONS
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


def present_result(name, result, arguments=None, *, workspace_cwd=None):
    value = dict(result)
    if name == "audit_get":
        # An explicit record read must retain its own original revision/origin.
        return value
    if name == "exec_command":
        summary = _test_summary(value, (arguments or {}).get("command", ""))
        if summary is not None:
            value["test_summary"] = summary
            # Keep the caller-budgeted original preview alongside the summary.
            # Hiding a small successful report forces another model turn just
            # to recover the execution facts already available in this call.
    if isinstance(value.get('command_result'), dict):
        value['command_result'] = present_result('exec_command', value['command_result'], arguments,
                                                 workspace_cwd=workspace_cwd)
    if name in {"workspace_switch", "workspace_use_local"}:
        redundant = {"thread_id", "created_at", "updated_at", "previous"}
        value = {key: item for key, item in value.items() if key not in redundant}
        if isinstance(value.get("initial"), dict):
            value["initial"] = present_result("exec_command", value["initial"], arguments,
                                              workspace_cwd=workspace_cwd)
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
        # Origin/recovery details stay in the command record.
        origin_fields = {"node", "boot_id", "version", "pid", "start_token", "task_id", "task_dir",
                         "backend", "timeout_seconds", "workload_pid", "pgid", "state", "reason", "exit_code"}
        value["remote_task"] = {key: item for key, item in task.items() if key not in origin_fields}
        projected = value["remote_task"]
        actual = projected.get("actual_cwd")
        launch = projected.get("launch_cwd")
        if actual and launch == actual:
            projected.pop("launch_cwd")
        if "origin_nonce" in projected and projected["origin_nonce"] is None:
            projected.pop("origin_nonce")
        if (name in {"exec_command", "inspect_batch"} and value.get("output_id") and not value.get("stderr")
                and not projected.get("origin_nonce") and projected.get("cleanup_state") == "process_group_signalled"
                and projected.get("cleanup_scope") == "process_group" and projected.get("tree_confirmed") is False
                and projected.get("hard_process_limit") is False):
            # The ordinary process-group policy is documented on the tool.
            # Keep non-default states and any new diagnostics on the wire.
            for key in ("cleanup_state", "cleanup_scope", "tree_confirmed", "hard_process_limit"):
                projected.pop(key)
            requested = (arguments or {}).get("cwd")
            expected = value.get("cwd")
            if not expected and (workspace_cwd or (isinstance(requested, str) and requested.startswith("/"))):
                expected = posixpath.normpath(posixpath.join(workspace_cwd or "/", requested or "."))
            if actual and actual == expected and launch in (None, actual):
                projected.pop("actual_cwd")
        if not projected:
            value.pop("remote_task")
    for key in ("stdout", "stderr"):
        if value.get(key) == "":
            value.pop(key)
    for key in ("timed_out", "truncated", "diff_truncated"):
        if value.get(key) is False:
            value.pop(key)
    return value
