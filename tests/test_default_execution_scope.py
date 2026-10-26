"""Acceptance coverage for execution facts without implicit test management.

Every SSH call uses a local shim. The fixture starts with an independent Git
repository so pytest's temporary parent cannot become the project boundary.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
import shlex
import sqlite3
import subprocess
import sys
import time

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
import pytest
import yaml


THREAD_ID = "default-execution-scope-thread"


def _write_ssh_shim(bin_dir: Path, *, remote_home: Path, safe_shell: Path,
                    event_log: Path) -> None:
    source = f"""#!{sys.executable}
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import ast
import base64
import zlib
import re

REMOTE_HOME = {str(remote_home)!r}
SAFE_SHELL = {str(safe_shell)!r}
EVENT_LOG = {str(event_log)!r}

def append_event(value):
    with open(EVENT_LOG, "a", encoding="utf-8") as output:
        output.write(json.dumps(value, separators=(",", ":")) + "\\n")

args = sys.argv[1:]
if args[:2] == ["-O", "check"]:
    raise SystemExit(0)
if len(args) < 2:
    raise SystemExit(2)
target, command = args[-2], args[-1]
env = dict(os.environ, HOME=REMOTE_HOME, SHELL=SAFE_SHELL,
           REMOTE_WORKBENCH_LOGIN_SHELL=SAFE_SHELL)
python_dir = {str(Path(sys.executable).parent)!r}
env["PATH"] = {str(bin_dir)!r} + os.pathsep + python_dir + os.pathsep + env.get("PATH", "")
process = subprocess.Popen(["/bin/bash", "--noprofile", "--norc", "-c", command],
                           stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, env=env, bufsize=0)
request = {{}}
request_bytes = bytearray()

def observe_request(chunk):
    if request:
        return
    request_bytes.extend(chunk)
    try:
        decoded = request_bytes.decode("utf-8")
        value, _ = json.JSONDecoder().raw_decode(decoded.lstrip())
    except (UnicodeDecodeError, ValueError):
        return
    if not isinstance(value, dict) or not isinstance(value.get("op"), str):
        return
    request.update(value)
    append_event({{
        "target": target,
        "op": value.get("op"),
        "job_id": value.get("job_id"),
        "command": value.get("command"),
        "path": value.get("path"),
        "project_root": value.get("project_root"),
        "max_files": value.get("max_files"),
        "max_bytes": value.get("max_bytes"),
    }})

def command_argv(current_command):
    for _ in range(4):
        match = re.search(r'base64\\.b64decode\\("([A-Za-z0-9+/=]+)"\\)', current_command)
        if not match:
            return None
        try:
            data = base64.b64decode(match.group(1), validate=True)
            if "exec(zlib.decompress(base64.b64decode(" in current_command:
                data = zlib.decompress(data)
            program = data.decode("utf-8")
            tree = ast.parse(program)
            nested_command = None
            for node in ast.walk(tree):
                if not isinstance(node, ast.Assign):
                    continue
                names = [target.id for target in node.targets if isinstance(target, ast.Name)]
                if "argv" in names:
                    return ast.literal_eval(node.value)
                if "_settings" in names:
                    settings = ast.literal_eval(node.value)
                    if isinstance(settings, dict):
                        nested_command = settings.get("command")
            if not isinstance(nested_command, str):
                return None
            current_command = nested_command
        except (ValueError, TypeError, SyntaxError, UnicodeDecodeError):
            return None
    return None

def fingerprint_request():
    try:
        argv = command_argv(command)
        if not isinstance(argv, list) or len(argv) < 6:
            return None
        if argv[0:2] != ["python3", "-c"] or argv[3] != "fingerprint":
            return None
        budget = json.loads(argv[5])
        return {{"target": target, "op": "fingerprint",
                "project_root": argv[4], "max_files": budget.get("max_files"),
                "max_bytes": budget.get("max_bytes")}}
    except (ValueError, TypeError, SyntaxError, UnicodeDecodeError):
        return None

def relay_input():
    reader = getattr(sys.stdin.buffer, "read1", sys.stdin.buffer.read)
    while True:
        chunk = reader(4096)
        if not chunk:
            break
        observe_request(chunk)
        try:
            process.stdin.write(chunk)
            process.stdin.flush()
        except BrokenPipeError:
            break
    try:
        process.stdin.close()
    except BrokenPipeError:
        pass

def relay_stdout():
    reader = getattr(process.stdout, "read1", process.stdout.read)
    while chunk := reader(65536):
        sys.stdout.buffer.write(chunk)
        sys.stdout.buffer.flush()

def relay_stderr():
    reader = getattr(process.stderr, "read1", process.stderr.read)
    while chunk := reader(65536):
        sys.stderr.buffer.write(chunk)
        sys.stderr.buffer.flush()

threads = [threading.Thread(target=relay_input, daemon=True),
           threading.Thread(target=relay_stdout, daemon=True),
           threading.Thread(target=relay_stderr, daemon=True)]
for thread in threads:
    thread.start()
return_code = process.wait()
for thread in threads:
    thread.join(timeout=3)
if not request:
    direct = fingerprint_request()
    if direct:
        append_event(direct)
    else:
        append_event({{"target": target, "op": "shell_command", "command_head": command[:200]}})
raise SystemExit(return_code)
"""
    shim = bin_dir / "ssh"
    shim.write_text(source, encoding="utf-8")
    shim.chmod(0o700)
    python_launcher = bin_dir / "python3"
    python_launcher.write_text(
        f"#!/bin/sh\nexec {shlex.quote(str(Path(sys.prefix) / 'bin' / 'python'))} \"$@\"\n",
        encoding="utf-8",
    )
    python_launcher.chmod(0o700)


def _init_fixture_repo(path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "add", "."], check=True)
    subprocess.run([
        "git", "-C", str(path), "-c", "user.name=scope acceptance",
        "-c", "user.email=scope-acceptance@example.invalid", "commit", "-qm",
        "fixture",
    ], check=True)


def _events(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line]


def _registry_rows(path: Path) -> dict[str, list[tuple]]:
    with sqlite3.connect(path) as db:
        return {
            "last_tests": db.execute(
                "SELECT * FROM last_tests ORDER BY workspace_id, target_id"
            ).fetchall(),
            "test_attempts": db.execute(
                "SELECT * FROM test_attempts ORDER BY attempt_order, attempt_id"
            ).fetchall(),
        }


def _assert_no_test_management(value: dict) -> None:
    encoded = json.dumps(value, ensure_ascii=False).lower()
    for forbidden in (
        "last_test", "latest_test_attempt", "test_attempt", "test_evidence",
        "current_code_verified", "current_code_checked", "applies_to_current_code",
        "current_applicability", "environment_verified_current",
    ):
        assert forbidden not in encoded, f"default result exposed {forbidden}: {value!r}"


def _value(result):
    assert not result.isError, result
    assert result.content, f"MCP returned no content: {result!r}"
    return json.loads(result.content[0].text)


async def _call(client: ClientSession, tool: str, arguments: dict | None = None):
    from remote_workbench.tool_interface import TOOL_NAMES
    if tool not in TOOL_NAMES and tool != "workspace_tools":
        arguments = {"tool": tool, "arguments": arguments or {}}
        tool = "workspace_tools"
    result = await client.call_tool(tool, arguments or {}, meta={"threadId": THREAD_ID})
    return _value(result)


def _write_evidence(path: Path, filename: str, value: dict) -> None:
    path.mkdir(parents=True, exist_ok=True)
    destination = path / filename
    temporary = destination.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    temporary.replace(destination)


@pytest.mark.asyncio
async def test_default_execution_paths_leave_legacy_test_history_untouched(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    source = tmp_path / "source"
    source.mkdir()
    source.joinpath("guarded.txt").write_text("original\n", encoding="utf-8")
    source.joinpath("patch-target.txt").write_text("before\n", encoding="utf-8")
    source.joinpath("native-patch.txt").write_text("native-before\n", encoding="utf-8")
    source.joinpath("test_scope.py").write_text(
        "import unittest\n"
        "class TestScope(unittest.TestCase):\n"
        "    def test_ordinary_execution(self):\n"
        "        print('UNITTEST_SCOPE_OK')\n",
        encoding="utf-8",
    )
    _init_fixture_repo(source)

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    remote_home = tmp_path / "remote-home"
    remote_home.mkdir()
    safe_shell = tmp_path / "safe-login-shell"
    safe_shell.write_text(
        "#!/bin/sh\n"
        "[ \"$1\" = \"-i\" ] && [ \"$2\" = \"-c\" ] || exit 99\n"
        "shift 2\n"
        "exec /bin/bash --noprofile --norc -c \"$1\"\n",
        encoding="utf-8",
    )
    safe_shell.chmod(0o700)
    event_log = tmp_path / "ssh-events.jsonl"
    _write_ssh_shim(bin_dir, remote_home=remote_home, safe_shell=safe_shell,
                    event_log=event_log)
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ.get("PATH", ""))

    config = tmp_path / "hosts.yaml"
    config.write_text(yaml.safe_dump({
        "default_host": "simulated",
        "hosts": {
            "simulated": {
                "ssh_host": "simulated-scope",
                "default_cwd": str(source),
                "execution_enabled": True,
                "command_timeout": 30,
                "file_timeout": 15,
                "job_timeout": 45,
                "terminate_grace_seconds": 1,
            },
        },
    }, sort_keys=True), encoding="utf-8")
    state_dir = tmp_path / "mcp-state"
    registry_path = state_dir / "workspaces.sqlite3"
    params = StdioServerParameters(command=sys.executable, args=[
        "-m", "remote_workbench.session_server", "--config", str(config),
        "--data-dir", str(state_dir), "--viewer-port", "0",
    ])
    evidence_dir = Path(os.environ.get("RWB_ACCEPTANCE_EVIDENCE_DIR", str(tmp_path)))
    background_job_id = None
    stopped = False

    async with stdio_client(params) as (reader, writer):
        async with ClientSession(reader, writer) as client:
            await client.initialize()
            try:
                binding = await _call(client, "workspace_switch", {
                    "host": "simulated", "cwd": str(source),
                })
                assert binding["bound"] is True
                workspace_id = binding["workspace_id"]
                target_id = binding["target_id"]
                from remote_workbench.workspace_registry import Registry, WorkspaceTarget

                registry = Registry(registry_path)
                target = WorkspaceTarget(
                    target_id, workspace_id, binding["host"], binding["source_cwd"],
                    binding["cwd"], binding["project_root"],
                )
                attempt = registry.begin_test_attempt(
                    workspace_id, target, attempt_id="a" * 32,
                    job_id="legacy-acceptance-attempt", command="python3 -m unittest -v",
                    cwd=binding["cwd"], execution_kind="foreground", started_at=123.0,
                    before={"code": {"complete": True, "digest": "seed-before"}},
                    revision=binding["revision"],
                )
                registry.update_test_attempt(
                    workspace_id, attempt["attempt_id"], state="finished",
                    process_exit_status="success", exit_code=0, test_result="success",
                    summary={"tests_run": 1, "passed": 1},
                    after={"code": {"complete": True, "digest": "seed-after"}},
                    finished_at=124.0,
                )
                registry.save_test(workspace_id, target_id, {
                    "attempt_id": attempt["attempt_id"],
                    "attempt_order": attempt["attempt_order"],
                    "status": "legacy-success-marker",
                    "command": "python3 -m unittest -v",
                    "code": {"complete": True, "digest": "legacy-digest"},
                })
                rows_before = _registry_rows(registry_path)
                assert rows_before["last_tests"] and rows_before["test_attempts"]

                event_count_before_legacy = len(_events(event_log))
                legacy = await _call(client, "workspace_resume", {
                    "include_legacy_test_history": True,
                })
                assert len(_events(event_log)) == event_count_before_legacy
                history = legacy.get("legacy_test_history")
                assert isinstance(history, dict), legacy
                assert history.get("applicability") == "unknown"
                assert history.get("source") == "local_legacy_history"
                assert history["last_test"]["status"] == "legacy-success-marker"
                assert history["last_test"].get("current_applicability") == "unknown"
                assert history["latest_test_attempt"]["attempt_id"] == attempt["attempt_id"]
                assert history["latest_test_attempt"].get("current_applicability") == "unknown"
                assert history["latest_test_attempt"].get("applies_to_current_code") is None
                assert _registry_rows(registry_path) == rows_before

                resume_before = await _call(client, "workspace_resume")
                _assert_no_test_management(resume_before)
                assert resume_before["remote_query_performed"] is False
                snapshot_before = await _call(client, "workspace_snapshot")
                _assert_no_test_management(snapshot_before)
                assert "content_fingerprint" not in snapshot_before
                assert "content_scan_budget" not in snapshot_before

                fingerprint_events_before = [
                    event for event in _events(event_log) if event.get("op") == "fingerprint"
                ]
                assert fingerprint_events_before == []

                script_command = "python3 -c \"print('SCRIPT_SCOPE_OK')\""
                script = await _call(client, "exec_command", {"command": script_command})
                _assert_no_test_management(script)
                assert script["exit_code"] == 0 and script["output_id"]
                script_audit = await _call(client, "audit_get", {"operation_id": script["output_id"]})
                assert script_audit["id"] == script["output_id"]
                assert script_audit["command"] == script_command
                script_output = await _call(client, "read_output", {
                    "output_id": script["output_id"], "stream": "stdout",
                })
                assert "SCRIPT_SCOPE_OK" in script_output["content"]

                unittest_command = "python3 -m unittest -v"
                unittest_result = await _call(client, "exec_command", {"command": unittest_command})
                _assert_no_test_management(unittest_result)
                assert unittest_result["exit_code"] == 0, unittest_result
                unittest_audit = await _call(client, "audit_get", {
                    "operation_id": unittest_result["output_id"],
                })
                assert unittest_audit["command"] == unittest_command
                unittest_stdout = await _call(client, "read_output", {
                    "output_id": unittest_result["output_id"], "stream": "stdout",
                })
                unittest_stderr = await _call(client, "read_output", {
                    "output_id": unittest_result["output_id"], "stream": "stderr",
                })
                assert "UNITTEST_SCOPE_OK" in unittest_stdout["content"]
                assert "Ran 1 test" in unittest_stderr["content"]

                pytest_command = "python3 -m pytest -q"
                pytest_result = await _call(client, "exec_command", {"command": pytest_command})
                _assert_no_test_management(pytest_result)
                assert pytest_result["exit_code"] == 0, pytest_result
                assert pytest_result["output_id"]

                original_sha = hashlib.sha256(b"original\n").hexdigest()
                write_result = await _call(client, "write_file", {
                    "path": "guarded.txt", "content": "written\n",
                    "expected_sha256": original_sha,
                })
                _assert_no_test_management(write_result)
                assert write_result.get("success") is True, write_result
                stale_write = await _call(client, "write_file", {
                    "path": "guarded.txt", "content": "stale\n",
                    "expected_sha256": original_sha,
                })
                assert stale_write.get("error") == "conflict", stale_write
                guarded = await _call(client, "read_file", {"path": "guarded.txt"})
                assert "written" in guarded.get("content", "")
                assert guarded.get("sha256") == hashlib.sha256(b"written\n").hexdigest()

                patch_before = hashlib.sha256(b"before\n").hexdigest()
                checked_patch = await _call(client, "patch_file", {
                    "path": "patch-target.txt", "old": "before", "new": "checked",
                    "expected_sha256": patch_before, "check_only": True,
                })
                assert checked_patch.get("success") is True and checked_patch.get("check_only") is True
                still_before = await _call(client, "read_file", {"path": "patch-target.txt"})
                assert "before" in still_before.get("content", "")
                assert still_before.get("sha256") == patch_before
                applied_patch = await _call(client, "patch_file", {
                    "path": "patch-target.txt", "old": "before", "new": "after",
                    "expected_sha256": patch_before,
                })
                assert applied_patch.get("success") is True and applied_patch.get("check_only") is False
                stale_patch = await _call(client, "patch_file", {
                    "path": "patch-target.txt", "old": "after", "new": "stale",
                    "expected_sha256": patch_before,
                })
                assert stale_patch.get("error") == "conflict", stale_patch
                patch_after = await _call(client, "read_file", {"path": "patch-target.txt"})
                assert "after" in patch_after.get("content", "")

                context_patch = (
                    "*** Begin Patch\n"
                    "*** Update File: native-patch.txt\n"
                    "@@\n"
                    "-native-before\n"
                    "+native-after\n"
                    "*** End Patch\n"
                )
                context_check = await _call(client, "workspace_tools", {"tool":"apply_patch", "arguments":{
                    "patch": context_patch, "check_only": True,
                }})
                assert context_check.get("success") is True and context_check.get("check_only") is True
                native_before = await _call(client, "read_file", {"path": "native-patch.txt"})
                assert "native-before" in native_before.get("content", "")
                context_apply = await _call(client, "workspace_tools", {"tool":"apply_patch", "arguments":{"patch":context_patch}})
                assert context_apply.get("success") is True and context_apply.get("check_only") is False
                native_after = await _call(client, "read_file", {"path": "native-patch.txt"})
                assert "native-after" in native_after.get("content", "")

                job_command = (
                    "python3 -c \"import time; print('BG_SCOPE_STARTED', flush=True); "
                    "time.sleep(30)\""
                )
                started = await _call(client, "start_job", {
                    "command": job_command, "idempotency_key": "default-scope-job",
                })
                _assert_no_test_management(started)
                assert started.get("job_id") and started.get("job_output_id"), started
                background_job_id = started["job_id"]
                assert started.get("launch_output_id")
                launch_audit = await _call(client, "audit_get", {
                    "operation_id": started["launch_output_id"],
                })
                assert launch_audit["id"] == started["launch_output_id"]
                launch_input = await _call(client, "read_output", {
                    "output_id": started["launch_output_id"], "stream": "stdin",
                })
                assert json.loads(launch_input["content"])["command"] == job_command

                resolved = await _call(client, "resolve_operation", {
                    "idempotency_key": "default-scope-job",
                })
                _assert_no_test_management(resolved)
                assert resolved["job_id"] == background_job_id
                assert resolved["local_job"]["job_output_id"] == started["job_output_id"]

                waited = await _call(client, "job_wait", {
                    "job_id": background_job_id, "wait_seconds": 3,
                    "stdout_cursor": 0, "stderr_cursor": 0, "max_bytes": 1024,
                })
                _assert_no_test_management(waited)
                assert "BG_SCOPE_STARTED" in waited.get("stdout_tail", ""), waited
                status = await _call(client, "job_status", {
                    "job_id": background_job_id,
                    "stdout_cursor": waited["next_stdout_cursor"],
                    "stderr_cursor": waited["next_stderr_cursor"],
                    "max_bytes": 1024,
                })
                _assert_no_test_management(status)
                assert status["job_output_id"] == started["job_output_id"]
                assert "BG_SCOPE_STARTED" not in status.get("stdout_tail", "")

                stopped_job = await _call(client, "stop_job", {"job_id": background_job_id})
                _assert_no_test_management(stopped_job)
                assert stopped_job.get("cancel_requested") is True, stopped_job
                stopped = True
                terminal_wait = await _call(client, "job_wait", {
                    "job_id": background_job_id, "wait_seconds": 3,
                    "stdout_cursor": waited["next_stdout_cursor"],
                    "stderr_cursor": waited["next_stderr_cursor"], "max_bytes": 1024,
                })
                _assert_no_test_management(terminal_wait)
                assert terminal_wait["job_id"] == background_job_id

                before_explicit_scan = len([
                    event for event in _events(event_log) if event.get("op") == "fingerprint"
                ])
                explicit_snapshot = await _call(client, "workspace_snapshot", {
                    "scan_content": True, "max_files": 13, "max_content_bytes": 4096,
                })
                _assert_no_test_management(explicit_snapshot)
                assert isinstance(explicit_snapshot.get("content_fingerprint"), dict)
                assert explicit_snapshot.get("content_scan_budget") == {
                    "max_files": 13, "max_content_bytes": 4096,
                }
                fingerprint_events_after = [
                    event for event in _events(event_log) if event.get("op") == "fingerprint"
                ]
                assert len(fingerprint_events_after) == before_explicit_scan + 1
                assert fingerprint_events_after[-1]["max_files"] == 13
                assert fingerprint_events_after[-1]["max_bytes"] == 4096
                assert fingerprint_events_after[-1]["project_root"] == binding["project_root"]

                default_resume_after = await _call(client, "workspace_resume")
                _assert_no_test_management(default_resume_after)
                default_snapshot_after = await _call(client, "workspace_snapshot")
                _assert_no_test_management(default_snapshot_after)
                assert "content_fingerprint" not in default_snapshot_after
                assert _registry_rows(registry_path) == rows_before

                events = _events(event_log)
                fingerprint_events = [event for event in events if event.get("op") == "fingerprint"]
                assert len(fingerprint_events) == 1
                _write_evidence(evidence_dir, "phase1-default-scope-evidence.json", {
                    "thread_id": THREAD_ID,
                    "workspace_id": workspace_id,
                    "target_id": target_id,
                    "default_fingerprint_calls": 0,
                    "explicit_fingerprint_calls": len(fingerprint_events),
                    "explicit_budget": {
                        "max_files": fingerprint_events[-1]["max_files"],
                        "max_content_bytes": fingerprint_events[-1]["max_bytes"],
                    },
                    "legacy_rows_unchanged": True,
                    "legacy_attempt_rows": len(rows_before["test_attempts"]),
                    "legacy_last_test_rows": len(rows_before["last_tests"]),
                    "ssh_events": events,
                })
            finally:
                if background_job_id and not stopped:
                    try:
                        await asyncio.wait_for(_call(client, "stop_job", {
                            "job_id": background_job_id,
                        }), timeout=10)
                    except Exception:
                        pass
