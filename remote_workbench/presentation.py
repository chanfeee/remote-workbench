"""Small, truthful summaries for local operation and output records.

These helpers never access the filesystem or SSH. Callers should pass only the
bounded text page they already have; ``complete`` says whether that text is the
entire stream.
"""
from __future__ import annotations

import posixpath
import re
import shlex
from typing import Any


_STATE_LABELS = {
    "success": "成功",
    "completed": "已完成",
    "failed": "失败",
    "skipped": "已跳过",
    "running": "运行中",
    "timeout": "已超时",
    "checked": "检查通过",
    "unknown": "状态未知",
}

_TOOL_TITLES = {
    "workspace_resume": "恢复工作进度",
    "workspace_note": "保存工作要点",
    "workspace_changes": "查看本会话改动",
    "workspace_preview": "预览交付改动",
    "workspace_apply": "交付已审阅改动",
    "resolve_operation": "恢复原任务句柄",
    "ssh_job_wait": "等待任务进展",
    "ssh_search_output": "搜索已保存输出",
    "workspace_snapshot": "查看工程快照",
    "session_identity_probe": "检查对话身份",
    "workspace_switch": "切换工作区",
    "prepare_workspace": "创建 session 工程副本",
    "ssh_check_connection": "检查远端连接",
    "ssh_run_background": "启动后台任务",
    "ssh_job_status": "查看后台任务",
    "ssh_stop_job": "停止后台任务",
    "ssh_read_file": "查看文件",
    "ssh_fetch_image": "拉取图片",
    "ssh_write_file": "写入文件",
    "ssh_append_file": "追加文件内容",
    "ssh_insert_lines": "修改文件",
    "ssh_replace_lines": "修改文件",
    "ssh_patch_file": "修改文件",
    "ssh_apply_patch": "应用代码补丁",
    "ssh_list_dir": "查看目录",
    "ssh_file_info": "查看文件信息",
    "ssh_mkdir": "创建目录",
    "ssh_move": "移动文件",
    "ssh_delete": "删除文件",
    "ssh_download": "下载文件",
    "ssh_grep": "搜索代码",
    "ssh_find_files": "查找文件",
    "ssh_git_status": "检查工作区",
    "ssh_git_diff": "查看代码改动",
    "ssh_git_log": "查看提交记录",
    "ssh_read_output": "读取完整输出",
    "ssh_audit_list": "查看操作记录",
    "ssh_audit_get": "查看操作详情",
}

_TEST_COMMAND = re.compile(
    r"(?:^|[\s;&|])(?:pytest(?:\s|$)|python(?:\d(?:\.\d+)?)?\s+-m\s+unittest\b|"
    r"python(?:\d(?:\.\d+)?)?\s+[^\s]*unittest|unittest\b|tox\b|nox\b|"
    r"go\s+test\b|cargo\s+test\b|npm\s+test\b|pnpm\s+test\b|yarn\s+test\b)",
    re.IGNORECASE,
)


def is_test_command(command):
    return bool(_TEST_COMMAND.search(command))

_UNITTEST_SUMMARY = re.compile(
    r"(?m)^Ran\s+(\d+)\s+tests?\s+in\s+([0-9]+(?:\.[0-9]+)?)s[ \t]*\r?\n"
    r"(?:[ \t]*\r?\n)*[ \t]*(OK(?:\s+\([^\r\n)]*\))?|FAILED(?:\s+\([^\r\n)]*\))?)[ \t]*\r?\n?[ \t\r\n]*\Z"
)
_UNITTEST_FAILURE = re.compile(r"(?m)^(?:FAIL|ERROR):\s*(.+?)\s*$")
_UNITTEST_COUNTER = re.compile(
    r"(?P<name>failures|errors|skipped|expected failures|unexpected successes)=(?P<count>\d+)"
)


def _mapping(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _first(*values: Any) -> str:
    for value in values:
        text = _text(value)
        if text:
            return text
    return ""


def _tool_operation(tool: str, args: dict) -> str:
    """Describe a worker-backed operation without exposing its Python wrapper."""
    path = _first(args.get("path"), args.get("file"))
    names = {
        "ssh_read_file": "读取文件",
        "ssh_write_file": "写入文件",
        "ssh_append_file": "追加文件",
        "ssh_insert_lines": "插入文件行",
        "ssh_replace_lines": "替换文件行",
        "ssh_patch_file": "修改文件",
        "ssh_file_info": "查看文件信息",
        "ssh_list_dir": "列出目录",
        "ssh_find_files": "查找文件",
        "ssh_grep": "搜索文件",
        "ssh_mkdir": "创建目录",
        "ssh_move": "移动文件",
        "ssh_delete": "删除文件",
        "ssh_download": "下载文件",
        "ssh_apply_patch": "应用代码补丁",
    }
    operation = names.get(tool, _TOOL_TITLES.get(tool, "执行操作"))
    return f"{operation}：{path}" if path else operation


def _status(record: dict, source: dict) -> str:
    """Resolve state with explicit tool/patch errors and exit codes first."""
    if source.get("kind") == "job":
        return _status(source, {})
    status = _first(record.get("status"), source.get("status")).lower()
    execution_state = _first(record.get("execution_state"), source.get("execution_state")).lower()
    patch_result = _first(record.get("patch_result"), source.get("patch_result")).lower()
    check_only = bool(record.get("check_only", source.get("check_only", False)))
    exit_code = record.get("exit_code")
    if exit_code is None:
        exit_code = source.get("exit_code")

    if record.get("error") or source.get("error") or patch_result == "failed" or status == "failed":
        return "failed"
    if status == "timeout":
        return "timeout"
    if record.get("timed_out") or source.get("timed_out"):
        return "timeout"
    if execution_state in {"unknown", "remote_state_unknown", "cancellation_requested"}:
        return "unknown"
    if status in {"starting", "running"}:
        return "running"
    if isinstance(exit_code, int) and exit_code != 0:
        return "failed"
    if patch_result == "checked" or check_only:
        return "checked"
    if status in {"cancelled", "unknown"} or execution_state == "remote_state_unknown":
        return "unknown"
    if exit_code == 0 or status in {"finished", "success", "succeeded"}:
        return "success"
    return "unknown"


def _elapsed(record: dict, source: dict) -> str:
    value = record.get("elapsed_ms")
    if value is None:
        value = source.get("elapsed_ms")
    if not isinstance(value, (int, float)) or value < 0:
        return "—"
    if value < 1000:
        return f"{int(round(value))} 毫秒"
    seconds = value / 1000
    seconds_text = f"{seconds:.3f}".rstrip("0").rstrip(".")
    return f"{seconds_text} 秒"


def _workspace_path(record: dict, source: dict, args: dict) -> str:
    return _first(
        record.get("actual_cwd"), source.get("actual_cwd"),
        record.get("cwd"), source.get("cwd"), args.get("cwd"),
    )


def _display_command(record: dict, source: dict, tool: str, args: dict) -> str:
    # For ordinary execution tools, the supplied command is the user's command.
    if tool in {"ssh_run", "ssh_run_background"}:
        return _first(args.get("command"), record.get("command"), source.get("command")) or "—"

    # Worker calls are serialized Python requests; show the file operation that
    # the user asked for instead of that transport implementation detail.
    worker_op = _first(record.get("worker_op"), source.get("worker_op"))
    if worker_op:
        return _tool_operation(tool, args)

    # Git/search/file tools have a concise user-facing action in their inputs.
    if tool in _TOOL_TITLES:
        if tool in {"ssh_grep", "ssh_find_files"}:
            query = _first(args.get("pattern"))
            path = _first(args.get("path"))
            base = "搜索代码" if tool == "ssh_grep" else "查找文件"
            return f"{base}：{query}（{path}）" if query and path else f"{base}：{query or path}" if query or path else base
        if tool == "ssh_git_diff":
            path = _first(args.get("path"))
            return f"查看代码改动：{path}" if path else "查看代码改动"
        if tool == "ssh_apply_patch":
            return _tool_operation(tool, args)
        return _tool_operation(tool, args)

    # A command child record (or a wrapped MCP record) is useful when no
    # structured tool arguments are available.
    return _first(record.get("command"), source.get("command")) or "—"


def _title(record: dict, source: dict, tool: str, args: dict) -> str:
    if tool in {"ssh_run", "ssh_run_background"}:
        command = _first(args.get("command"), record.get("command"), source.get("command"))
        if _TEST_COMMAND.search(command):
            return "运行测试"
        lowered = command.lower()
        if re.search(r"(?:^|\s)git\s+(?:--no-pager\s+)?diff\b", lowered):
            return "查看代码改动"
        if re.search(r"(?:^|\s)git\s+status\b", lowered):
            return "检查工作区"
        return _TOOL_TITLES.get(tool, "执行命令")

    title = _TOOL_TITLES.get(tool)
    path = _first(args.get("path"), args.get("file"))
    if title and path and tool in {
        "ssh_read_file", "ssh_write_file", "ssh_append_file", "ssh_insert_lines",
        "ssh_replace_lines", "ssh_patch_file", "ssh_list_dir", "ssh_file_info",
        "ssh_mkdir", "ssh_move", "ssh_delete", "ssh_download",
    }:
        return f"{title}：{path}"
    if title:
        return title

    command = _first(record.get("command"), source.get("command"))
    if command:
        if _TEST_COMMAND.search(command):
            return "运行测试"
        lowered = command.lower()
        if re.search(r"(?:^|\s)git\s+(?:--no-pager\s+)?diff\b", lowered):
            return "查看代码改动"
        if re.search(r"(?:^|\s)git\s+status\b", lowered):
            return "检查工作区"
        return "执行命令"
    return "操作记录"


def summarize_record(record: dict, source_record: dict | None = None) -> dict:
    """Return a compact Chinese view of an MCP operation and its command child.

    ``source_record`` is usually the actual command record linked from a tool
    record. Unknown paths and states remain unknown rather than being guessed.
    """
    record = _mapping(record)
    source = _mapping(source_record)
    args = _mapping(record.get("arguments"))
    tool = _first(record.get("tool"), source.get("tool"))
    path = _workspace_path(record, source, args)
    workspace = posixpath.basename(path.rstrip("/")) if path.rstrip("/") else ("/" if path == "/" else "未指定")
    state = _status(record, source)
    elapsed = _elapsed(record, source)
    view = {
        "title": _title(record, source, tool, args),
        "state": state,
        "state_label": _STATE_LABELS[state],
        "host": _first(record.get("host"), source.get("host")),
        "workspace": workspace,
        "workspace_path": path,
        "elapsed": elapsed,
        "command": _display_command(record, source, tool, args),
    }
    job = source if source.get("kind") == "job" else record
    if job.get("kind") == "job":
        sync = job.get("sync_state", "unknown")
        view["sync_state"] = sync
        view["sync_label"] = {"queued": "日志等待同步", "syncing": "日志同步中",
                              "complete": "日志已同步", "disconnected": "日志同步断开，任务状态待核实"}.get(sync, "日志同步状态未知")
    return view


def _record_exit_code(record: dict) -> int | None:
    candidate = record.get("exit_code")
    if candidate is None and isinstance(record.get("source_record"), dict):
        candidate = record["source_record"].get("exit_code")
    return candidate if isinstance(candidate, int) else None


def _contains_error(record: dict) -> bool:
    return bool(record.get("error")) or record.get("status") == "failed" or record.get("patch_result") == "failed"


def _unittest_counters(footer: str, total: int) -> dict | None:
    """Parse only unittest's known footer counters; unknown forms stay visible."""
    open_paren = footer.find(" (")
    if open_paren < 0:
        return {}
    if not footer.endswith(")"):
        return None
    raw = footer[open_paren + 2:-1]
    if not raw:
        return None
    counters = {}
    for item in raw.split(", "):
        match = _UNITTEST_COUNTER.fullmatch(item)
        if not match or match.group("name") in counters:
            return None
        counters[match.group("name")] = int(match.group("count"))

    expected_failures = counters.get("expected failures", 0)
    # unittest counts skipped subtests as well as skipped TestCase methods, so
    # the framework's skipped counter may exceed its top-level ``Ran N`` count.
    # Expected failures, in contrast, are reported per top-level test case.
    if expected_failures > total:
        return None
    if footer.startswith("OK") and any(
        counters.get(name, 0) for name in ("failures", "errors", "unexpected successes")
    ):
        return None
    return counters


def summarize_output(text: str, record: dict, stream: str, complete: bool = False) -> dict | None:
    """Summarize a complete Python ``unittest`` result when its ending is clear.

    Partial pages never become pass/fail claims. A pass also requires exit code
    zero; a valid ``FAILED`` footer remains a failure even if a wrapper reports
    an inconsistent zero exit code.
    """
    if not complete or stream not in {"stdout", "stderr"} or not isinstance(text, str):
        return None
    record = _mapping(record)
    source = _mapping(record.get("source_record"))
    exit_code = _record_exit_code(record)
    if record.get("timed_out") or source.get("timed_out"):
        return None
    if (record.get("status") in {"running", "starting", "timeout", "cancelled", "unknown"}
            or record.get("execution_state") in {"unknown", "remote_state_unknown", "cancellation_requested"}
            or source.get("execution_state") in {"unknown", "remote_state_unknown", "cancellation_requested"}):
        return None
    if _contains_error(record) and not _UNITTEST_SUMMARY.search(text):
        return None

    match = _UNITTEST_SUMMARY.search(text)
    if not match:
        return None
    count = int(match.group(1))
    seconds = float(match.group(2))
    footer = match.group(3)
    reported_success = footer.startswith("OK")
    counters = _unittest_counters(footer, count)
    if counters is None or count == 0:
        return None
    skipped = counters.get("skipped", 0)
    expected_failures = counters.get("expected failures", 0)
    # ``Ran N`` is unittest's top-level testsRun count. Its skip counter also
    # includes subTest skips, so it cannot be subtracted from N to infer how
    # many top-level tests executed or passed. With no reported skips, the
    # top-level execution count is exact; an OK footer then lets us subtract
    # expected failures from the pass count.
    executed = count if skipped == 0 else None
    passed = executed - expected_failures if reported_success and executed is not None else None
    operation_failed = _contains_error(record) or exit_code not in (None, 0)
    if operation_failed:
        process_status = "failed"
    elif exit_code == 0:
        process_status = "success"
    else:
        process_status = "unknown"
    if reported_success and exit_code is None and not _contains_error(record):
        return None
    if reported_success and operation_failed:
        state = "failed"
        title = "测试报告通过，但任务执行失败"
        if exit_code not in (None, 0):
            detail = f"报告耗时 {seconds:.3f} 秒；进程退出码 {exit_code}"
        else:
            detail = f"报告耗时 {seconds:.3f} 秒；操作状态为失败"
    elif reported_success and (skipped or expected_failures):
        state = "completed"
        details = [f"框架运行 {count} 项"]
        if skipped:
            details.append(f"框架报告跳过 {skipped} 项")
        if expected_failures:
            details.append(f"预期失败 {expected_failures} 项")
        if executed is None:
            details.append("执行数和通过数未知")
        else:
            details.append(f"执行 {executed} 项，通过 {passed} 项")
        title = "测试报告完成（" + "，".join(details) + "）"
        detail = f"耗时 {seconds:.3f} 秒；进程成功退出"
    elif reported_success:
        state = "success"
        title = f"{count} 项测试通过"
        detail = f"耗时 {seconds:.3f} 秒"
    else:
        state = "failed"
        title = f"测试未通过（共 {count} 项）"
        detail = f"耗时 {seconds:.3f} 秒；{footer}"

    failures = _UNITTEST_FAILURE.findall(text)
    result = {
        "kind": "tests",
        "title": title,
        "state": state,
        "count": count,
        "tests_run": count,
        "reported_skips": skipped,
        "executed": executed,
        "passed": passed,
        "report_status": "success" if reported_success else "failed",
        "process_status": process_status,
        "detail": detail,
        "complete": True,
    }
    # Keep subtest/error names compact while retaining actionable pointers.
    if failures:
        result["failures"] = failures[:20]
        result["failure_count"] = len(failures)
    # Keep the older presentation aliases for current consumers. ``count``
    # and ``total`` both mean the framework's top-level count; ``skipped``
    # mirrors its reported skip-event count, now clarified by reported_skips.
    result.update({"total": count, "skipped": skipped})
    if expected_failures:
        result["expected_failures"] = expected_failures
    if counters.get("unexpected successes"):
        result["unexpected_successes"] = counters["unexpected successes"]
    if counters.get("failures"):
        result["reported_failures"] = counters["failures"]
    if counters.get("errors"):
        result["reported_errors"] = counters["errors"]
    return result


def _strip_git_prefix(path: str, prefix: str) -> str:
    path = path.strip()
    if path.startswith('"'):
        try:
            path = shlex.split(path)[0]
        except (ValueError, IndexError):
            path = path.strip('"')
    if path.startswith(prefix):
        path = path[len(prefix):]
    return path


def _header_path(value: str, prefix: str) -> str | None:
    value = value.split("\t", 1)[0].strip()
    if value == "/dev/null":
        return None
    return _strip_git_prefix(value, prefix)


def parse_unified_diff(text: str, complete: bool = False) -> dict | None:
    """Count changed lines and files in a bounded unified diff text page."""
    if not isinstance(text, str) or not text.strip():
        return None
    lines = text.splitlines()
    # Combined diffs use different line prefixes, so ordinary +/- counts would
    # be misleading. Let the viewer show the raw patch instead.
    if any(line.startswith("@@@") for line in lines):
        return None

    files: dict[str, dict[str, Any]] = {}
    current: dict[str, Any] | None = None
    old_path: str | None = None
    in_hunk = False
    old_remaining = 0
    new_remaining = 0
    saw_diff = False

    for line in lines:
        if line.startswith("diff --git "):
            saw_diff = True
            in_hunk = False
            old_path = None
            try:
                parts = shlex.split(line[len("diff --git "):])
            except ValueError:
                parts = []
            path = None
            # Quoted paths with spaces parse as two tokens. If the header is
            # ambiguous (for example, an unquoted path with spaces), defer the
            # file entry until the unambiguous ---/+++ headers below.
            if len(parts) == 2:
                path = _strip_git_prefix(parts[1], "b/")
                if path == parts[1]:
                    path = _strip_git_prefix(parts[0], "a/")
            if path:
                current = files.setdefault(path, {"path": path, "added": 0, "deleted": 0})
            else:
                current = None
            continue

        hunk = re.match(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", line)
        if hunk:
            saw_diff = True
            in_hunk = True
            old_remaining = int(hunk.group(2) or "1")
            new_remaining = int(hunk.group(4) or "1")
            continue
        if line.startswith("@@"):
            in_hunk = False
            continue

        if in_hunk:
            consumed = True
            if line.startswith("\\ No newline"):
                continue
            if line.startswith("+"):
                if current is not None:
                    current["added"] += 1
                new_remaining -= 1
            elif line.startswith("-"):
                if current is not None:
                    current["deleted"] += 1
                old_remaining -= 1
            elif line.startswith(" "):
                old_remaining -= 1
                new_remaining -= 1
            else:
                in_hunk = False
                consumed = False
            if in_hunk and old_remaining <= 0 and new_remaining <= 0:
                in_hunk = False
            if consumed:
                continue

        if line.startswith("--- "):
            saw_diff = True
            old_path = _header_path(line[4:], "a/")
            continue

        if line.startswith("+++ "):
            saw_diff = True
            new_path = _header_path(line[4:], "b/")
            path = new_path or old_path
            if path:
                current = files.setdefault(path, {"path": path, "added": 0, "deleted": 0})
            continue

    if not saw_diff or not files:
        return None
    rows = list(files.values())
    added = sum(item["added"] for item in rows)
    deleted = sum(item["deleted"] for item in rows)
    title = (
        f"共 {len(rows)} 个文件变更，新增 {added} 行、删除 {deleted} 行"
        if complete else f"已读到 {len(rows)} 个文件的 diff 片段（计数未完成）"
    )
    return {
        "kind": "diff",
        "title": title,
        "files": rows,
        "added": added,
        "deleted": deleted,
        "complete": bool(complete),
    }
