"""Model-facing projections; complete command/output records stay local.

Only known redundant metadata is omitted. Unknown fields, failures, warnings,
unknown execution state and cleanup uncertainty remain visible to the agent.
"""
import re

from remote_workbench import audit
from remote_workbench.presentation import is_test_command, summarize_output


COMPACT_TOOLS = frozenset({
    "workspace_info", "workspace_switch", "workspace_use_local", "exec_command",
    "apply_patch", "read_output", "fetch_image",
})

COMPACT_INSTRUCTIONS = (
    "Remote SSH tools; native tools stay local. Use only for a host/directory explicitly assigned "
    "by the user in this chat. New chats start local. workspace_switch(host,cwd,command=...) "
    "creates/resumes this chat's private copy and can run the first inspection in one call; "
    "no preceding workspace_info/probe is needed. Never infer intent from a saved binding. "
    "On identity/SSH failure stop; never run the remote task locally. "
    "Paths start at the private copy; commands use fresh account-default login shells. "
    "Activate environments in each command. No remote Codex CLI or automatic environment reuse. "
    "Use exec_command for inspection, reads/search, tests and Git; combine independent checks in one command. "
    "apply_patch accepts *** Begin Patch context format or unified Git diff. "
    "Review changed code and run relevant tests; success summaries never replace code review. "
    "Full output/diffs stay in the local operation log; read_output pages details when needed. "
    "Use workspace_tools for jobs, delivery, notes and recovery; describe one tool only when needed. "
    "Reuse the first returned operation_log Markdown link titled **Remote Workbench 操作记录** in updates "
    "and the final reply; a workspace link covers every operation, so do not collect individual links. "
    "For images use fetch_image and show image_markdown; never put image/base64 bytes in text. "
    "Missing threadId fails closed. Switching hosts preserves job origins. "
    "This is workspace separation, not an OS sandbox. "
    "Before substantial compute, batch/parallel jobs or spawn/preload tests: show exact command, "
    "concurrency, host/cwd and environment; wait for the user's explicit confirmation, then pass "
    "compute_review={concurrency:...,environment:...,confirmation:...}. Never invent confirmation. "
    "Inspect unclear fan-out. Ordinary small tests/file operations need no extra confirmation."
)

_PROGRESS = re.compile(
    r"(?:\s*test\S* .* \.\.\.(?:[ \t]+(?:ok|skipped .+|expected failure|unexpected success))?[ \t]*|"
    r"UNEXPECTED SUCCESS: .+|[.sx]+)"
)


def _test_summary(result, command):
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


def compact_result(name, result, arguments=None):
    value = dict(result)
    if name == "exec_command":
        summary = _test_summary(value, (arguments or {}).get("command", ""))
        if summary is not None:
            value["test_summary"] = summary
            if summary["status"] != "failed":
                value.pop("stdout", None)
                value.pop("stderr", None)
    if name in {"workspace_switch", "workspace_use_local"}:
        redundant = {"thread_id", "workspace_id", "project_root", "target_id",
                     "created_at", "updated_at", "previous"}
        value = {key: item for key, item in value.items() if key not in redundant}
        if isinstance(value.get("initial"), dict):
            value["initial"] = compact_result("exec_command", value["initial"], arguments)
    elif name not in {"workspace_info", "workspace_resume", "workspace_history"}:
        value.pop("workspace_id", None)
        value.pop("revision", None)
    if value.get("output_id"):
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
    for key in ("timed_out", "truncated", "diff_truncated", "check_only"):
        if value.get(key) is False:
            value.pop(key)
    return value
