"""C2 recovery with real MCP stdio processes and a local SSH shim.

The shim drops one successful job_start reply after the worker persisted its
job. A second MCP process switches to B, then recovers A's same job and output
handle. The shim never invokes a real SSH client or server.
"""
from __future__ import annotations

import asyncio
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


THREAD_ID = "stdio-recovery-thread"
IDEMPOTENCY_KEY = "stdio-recovery-once"


def _write_ssh_shim(bin_dir: Path, *, remote_home: Path, safe_shell: Path,
                    launch_counter: Path, event_log: Path, drop_gate: Path,
                    dropped_log: Path, release_gate: Path) -> None:
    source = f"""#!{sys.executable}
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import ast
import base64
import zlib

REMOTE_HOME = {str(remote_home)!r}
SAFE_SHELL = {str(safe_shell)!r}
LAUNCH_COUNTER = {str(launch_counter)!r}
EVENT_LOG = {str(event_log)!r}
DROP_GATE = {str(drop_gate)!r}
DROPPED_LOG = {str(dropped_log)!r}
RELEASE_GATE = {str(release_gate)!r}

def append_json(path, value):
    with open(path, "a", encoding="utf-8") as output:
        output.write(json.dumps(value, separators=(",", ":")) + "\\n")

args = sys.argv[1:]
if args[:2] == ["-O", "check"]:
    raise SystemExit(0)
if len(args) < 2:
    raise SystemExit(2)
target, command = args[-2], args[-1]
env = dict(os.environ, HOME=REMOTE_HOME, SHELL=SAFE_SHELL,
           REMOTE_WORKBENCH_LOGIN_SHELL=SAFE_SHELL,
           RWB_TEST_LAUNCH_COUNTER=LAUNCH_COUNTER,
           RWB_TEST_RELEASE_GATE=RELEASE_GATE)
python_dir = {str(Path(sys.executable).parent)!r}
env["PATH"] = {str(bin_dir)!r} + os.pathsep + python_dir + os.pathsep + env.get("PATH", "")
process = subprocess.Popen(["/bin/bash", "--noprofile", "--norc", "-c", command],
                           stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, env=env, bufsize=0)
request = {{}}
request_bytes = bytearray()
captured_stdout = bytearray()

def record_request(chunk):
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

def cancel_request():
    current_command = command
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
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "cancel_task":
                    if node.args:
                        parts = [item.value for item in ast.walk(node.args[0])
                                 if isinstance(item, ast.Constant) and isinstance(item.value, str)]
                        job_ids = [value for value in parts
                                   if re.fullmatch(r"[a-f0-9]{{32}}", value)]
                        if job_ids:
                            return {{"job_id": job_ids[0]}}
                    return {{"job_id": None}}
                if not isinstance(node, ast.Assign):
                    continue
                names = [target.id for target in node.targets if isinstance(target, ast.Name)]
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

def relay_input():
    reader = getattr(sys.stdin.buffer, "read1", sys.stdin.buffer.read)
    while True:
        chunk = reader(4096)
        if not chunk:
            break
        record_request(chunk)
        try:
            process.stdin.write(chunk)
            process.stdin.flush()
        except BrokenPipeError:
            break
    try:
        process.stdin.close()
    except BrokenPipeError:
        pass

def relay_stderr():
    reader = getattr(process.stderr, "read1", process.stderr.read)
    while chunk := reader(65536):
        sys.stderr.buffer.write(chunk)
        sys.stderr.buffer.flush()

def relay_stdout():
    reader = getattr(process.stdout, "read1", process.stdout.read)
    while chunk := reader(65536):
        if request.get("op") == "job_start":
            captured_stdout.extend(chunk)
        else:
            sys.stdout.buffer.write(chunk)
            sys.stdout.buffer.flush()

threads = [threading.Thread(target=relay_input, daemon=True),
           threading.Thread(target=relay_stderr, daemon=True),
           threading.Thread(target=relay_stdout, daemon=True)]
for thread in threads:
    thread.start()
return_code = process.wait()
for thread in threads:
    thread.join(timeout=3)
stdout = bytes(captured_stdout)
op = request.get("op")
job_dir = None
persisted = False
drop = False
if op == "job_start" and Path(DROP_GATE).exists() and return_code == 0:
    marker = re.search(rb"\\x1eREMOTE_WORKBENCH_RESULT_[0-9a-f]+:\\x1f", stdout)
    if marker:
        try:
            result, _ = json.JSONDecoder().raw_decode(stdout[marker.end():].decode("utf-8"))
            job_dir = result.get("job_dir")
            root = Path(job_dir) if isinstance(job_dir, str) else None
            persisted = bool(root and (root / "status.json").is_file()
                             and (root / "origin.json").is_file())
        except (ValueError, TypeError, OSError):
            persisted = False
    if persisted:
        try:
            os.unlink(DROP_GATE)
            drop = True
        except FileNotFoundError:
            pass
if drop:
    append_json(DROPPED_LOG, {{"op": op, "job_id": request.get("job_id"),
                               "job_dir": job_dir, "persisted": persisted,
                               "response_was_present": b"REMOTE_WORKBENCH_RESULT_" in stdout,
                               "command": request.get("command")}})
elif op == "job_start":
    sys.stdout.buffer.write(stdout)
    sys.stdout.buffer.flush()
direct = fingerprint_request() if op is None else None
if direct:
    inferred = "fingerprint"
    direct = dict(direct)
elif op is None and cancel_request() is not None:
    direct = cancel_request()
    inferred = "cancel_task"
elif op is None:
    inferred = ("cancel_task" if "cancel_task(" in command else
                "job_log_follow" if "follow_logs(" in command else "shell_command")
else:
    inferred = op
append_json(EVENT_LOG, {{"target": target, "op": inferred,
                        "job_id": (direct or {{}}).get("job_id", request.get("job_id")),
                        "command": request.get("command"),
                        "project_root": (direct or {{}}).get("project_root", request.get("project_root")),
                        "max_files": (direct or {{}}).get("max_files", request.get("max_files")),
                        "max_bytes": (direct or {{}}).get("max_bytes", request.get("max_bytes")),
                        "dropped": drop, "server_pid": os.getppid(),
                        "exit_code": return_code}})
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
    """Give each fixture an independent Git boundary, outside pytest's parent."""
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "add", "."], check=True)
    subprocess.run([
        "git", "-C", str(path), "-c", "user.name=stdio acceptance",
        "-c", "user.email=stdio-acceptance@example.invalid", "commit", "-qm",
        "fixture",
    ], check=True)


def _events(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line]


def _value(result):
    assert not result.isError, result
    assert result.content, f"MCP returned no content: {result!r}"
    return json.loads(result.content[0].text)


async def _call(client: ClientSession, tool: str, arguments: dict | None = None):
    result = await client.call_tool(tool, arguments or {}, meta={"threadId": THREAD_ID})
    return _value(result)


def _reserved_job_id(db_path: Path) -> str | None:
    if not db_path.exists():
        return None
    with sqlite3.connect(db_path) as db:
        row = db.execute(
            "SELECT job_id FROM job_operations WHERE idempotency_key = ?",
            (IDEMPOTENCY_KEY,),
        ).fetchone()
    return row[0] if row else None


def _registry_rows(db_path: Path) -> dict[str, list[tuple]]:
    with sqlite3.connect(db_path) as db:
        return {
            "last_tests": db.execute(
                "SELECT * FROM last_tests ORDER BY workspace_id, target_id"
            ).fetchall(),
            "test_attempts": db.execute(
                "SELECT * FROM test_attempts ORDER BY attempt_order, attempt_id"
            ).fetchall(),
        }


def _seed_legacy_rows(db_path: Path, binding: dict) -> None:
    from remote_workbench.workspace_registry import Registry, WorkspaceTarget

    registry = Registry(db_path)
    target = WorkspaceTarget(
        binding["target_id"], binding["workspace_id"], binding["host"],
        binding["source_cwd"], binding["cwd"], binding["project_root"],
    )
    attempt = registry.begin_test_attempt(
        binding["workspace_id"], target, attempt_id="b" * 32,
        job_id="legacy-stdio-attempt", command="python3 -m unittest -v",
        cwd=binding["cwd"], execution_kind="foreground", started_at=123.0,
        before={"code": {"complete": True, "digest": "seed-before"}},
        revision=binding["revision"],
    )
    registry.update_test_attempt(
        binding["workspace_id"], attempt["attempt_id"], state="finished",
        process_exit_status="success", exit_code=0, test_result="success",
        summary={"tests_run": 1, "passed": 1},
        after={"code": {"complete": True, "digest": "seed-after"}},
        finished_at=124.0,
    )
    registry.save_test(binding["workspace_id"], binding["target_id"], {
        "attempt_id": attempt["attempt_id"],
        "attempt_order": attempt["attempt_order"],
        "status": "legacy-success-marker",
        "command": "python3 -m unittest -v",
        "code": {"complete": True, "digest": "legacy-digest"},
    })


async def _wait_for_local_record(state_dir: Path, workspace_id: str, output_id: str,
                                 *, timeout: float, marker: str) -> dict:
    deadline = time.monotonic() + timeout
    last = None
    stdout_path = state_dir / "audit" / workspace_id / output_id / "stdout"
    record_path = state_dir / "audit" / workspace_id / output_id / "record.json"
    while time.monotonic() < deadline:
        try:
            last = json.loads(record_path.read_text(encoding="utf-8"))
            output = stdout_path.read_text(encoding="utf-8")
        except (OSError, ValueError):
            await asyncio.sleep(0.05)
            continue
        if marker in output:
            return last
        await asyncio.sleep(0.05)
    raise AssertionError(f"local collector did not receive {marker!r}: {last!r}")


async def _wait_for_local_terminal_record(state_dir: Path, workspace_id: str,
                                          output_id: str, *, timeout: float) -> dict:
    deadline = time.monotonic() + timeout
    record_path = state_dir / "audit" / workspace_id / output_id / "record.json"
    last = None
    while time.monotonic() < deadline:
        try:
            last = json.loads(record_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            await asyncio.sleep(0.05)
            continue
        if (last.get("sync_state") == "complete" and
                last.get("execution_state") in {"finished", "failed", "cancelled", "timed_out"}):
            return last
        await asyncio.sleep(0.05)
    raise AssertionError(f"local collector did not publish terminal state: {last!r}")


def _write_acceptance_evidence(path: Path, filename: str, value: dict) -> None:
    path.mkdir(parents=True, exist_ok=True)
    destination = path / filename
    temporary = destination.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    temporary.replace(destination)


@pytest.mark.asyncio
async def test_lost_job_start_reply_recovers_after_real_stdio_restart_and_target_switch(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    source_a = tmp_path / "source-a"
    source_b = tmp_path / "source-b"
    source_a.mkdir()
    source_b.mkdir()
    source_b.joinpath("test_b_only.py").write_text(
        "import os, unittest\n"
        "class WrongProject(unittest.TestCase):\n"
        " def test_must_not_run(self):\n"
        "  open(os.environ['RWB_TEST_LAUNCH_COUNTER'], 'a').write('B\\n')\n",
        encoding="utf-8",
    )
    source_a.joinpath("test_slow.py").write_text(
        "import os, time, unittest\n"
        "class OriginalProject(unittest.TestCase):\n"
        " def test_background_result(self):\n"
        "  print('ORIGINAL_PROJECT_STARTED', flush=True)\n"
        "  print('CURSOR_PREFIX_START' + 'p' * 500 + 'CURSOR_PREFIX_END', flush=True)\n"
        "  if os.environ.get('RWB_TEST_COUNT_LAUNCH', '1') != '0':\n"
        "   label = os.environ.get('RWB_TEST_LAUNCH_LABEL', 'A')\n"
        "   with open(os.environ['RWB_TEST_LAUNCH_COUNTER'], 'a') as output: output.write(label + '\\n')\n"
        "  if os.environ.get('RWB_TEST_BYPASS_GATE') != '1':\n"
        "   gate = os.environ['RWB_TEST_RELEASE_GATE']\n"
        "   deadline = time.monotonic() + 45\n"
        "   while os.path.exists(gate) and time.monotonic() < deadline: time.sleep(0.025)\n"
        "   if os.path.exists(gate): raise AssertionError('acceptance gate timed out')\n"
        "  print('ORIGINAL_PROJECT_RESULT', flush=True)\n"
        "  print('CURSOR_PAYLOAD_START' + 'x' * 400 + 'CURSOR_PAYLOAD_END', flush=True)\n",
        encoding="utf-8",
    )
    _init_fixture_repo(source_a)
    _init_fixture_repo(source_b)

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
    launch_counter = tmp_path / "launch-count.txt"
    event_log = tmp_path / "ssh-events.jsonl"
    drop_gate = tmp_path / "drop-first-job-start"
    dropped_log = tmp_path / "dropped-success.jsonl"
    release_gate = tmp_path / "hold-original-unittest"
    release_gate.touch()
    _write_ssh_shim(bin_dir, remote_home=remote_home, safe_shell=safe_shell,
                    launch_counter=launch_counter, event_log=event_log,
                    drop_gate=drop_gate, dropped_log=dropped_log,
                    release_gate=release_gate)
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ.get("PATH", ""))

    config = tmp_path / "hosts.yaml"
    config.write_text(yaml.safe_dump({
        "default_host": "a",
        "hosts": {
            "a": {"ssh_host": "simulated-a", "default_cwd": str(source_a),
                  "execution_enabled": True, "job_timeout": 60,
                  "command_timeout": 30, "terminate_grace_seconds": 1},
            "b": {"ssh_host": "simulated-b", "default_cwd": str(source_b),
                  "execution_enabled": True, "job_timeout": 60,
                  "command_timeout": 30, "terminate_grace_seconds": 1},
        },
    }, sort_keys=True), encoding="utf-8")
    state_dir = tmp_path / "mcp-state"
    registry_path = state_dir / "workspaces.sqlite3"
    params = StdioServerParameters(command=sys.executable, args=[
        "-m", "remote_workbench.session_server", "--config", str(config),
        "--data-dir", str(state_dir), "--viewer-port", "0",
    ])
    evidence_dir = Path(os.environ.get("RWB_ACCEPTANCE_EVIDENCE_DIR", str(tmp_path)))
    background_command = "python3 -m unittest -v"
    fast_command = (
        "RWB_TEST_BYPASS_GATE=1 RWB_TEST_COUNT_LAUNCH=0 "
        "python3 -m unittest -v"
    )
    job_id = None
    release_gate_removed = False
    stop_requested = False
    stop_terminal_confirmed = False
    stop_cleanup_confirmed = False
    rows_before = None
    output_id = None
    target_a_id = None
    workspace_id = None
    initial_start_pid = None

    try:
        async with stdio_client(params) as (reader, writer):
            async with ClientSession(reader, writer) as client:
                await client.initialize()
                binding_a = await _call(client, "workspace_switch", {
                    "host": "a", "cwd": str(source_a),
                })
                assert binding_a["bound"] is True
                target_a_id = binding_a["target_id"]
                workspace_id = binding_a["workspace_id"]
                _seed_legacy_rows(registry_path, binding_a)
                rows_before = _registry_rows(registry_path)
                assert rows_before["last_tests"] and rows_before["test_attempts"]

                resume = await _call(client, "workspace_resume")
                assert resume["remote_query_performed"] is False
                assert "last_test" not in resume and "latest_test_attempt" not in resume
                snapshot = await _call(client, "workspace_snapshot")
                assert "content_fingerprint" not in snapshot
                assert "test_evidence_comparison" not in snapshot
                assert "latest_test_attempt" not in snapshot
                assert not [e for e in _events(event_log) if e.get("op") == "fingerprint"]

                baseline_test = await _call(client, "exec_command", {"command": fast_command})
                assert baseline_test.get("exit_code") == 0, baseline_test
                assert baseline_test.get("output_id")
                assert "test_evidence" not in baseline_test and "test_attempt" not in baseline_test
                output_id = baseline_test["output_id"]
                baseline_audit = await _call(client, "audit_get", {"operation_id": output_id})
                assert baseline_audit["id"] == output_id
                assert baseline_audit["command"] == fast_command
                saved_stdout = await _call(client, "read_output", {
                    "output_id": output_id, "stream": "stdout",
                })
                saved_stderr = await _call(client, "read_output", {
                    "output_id": output_id, "stream": "stderr",
                })
                assert "ORIGINAL_PROJECT_STARTED" in saved_stdout["content"]
                assert "Ran 1 test" in saved_stderr["content"]
                assert _registry_rows(registry_path) == rows_before

                drop_gate.touch()
                start_result = {}
                raw_start = await client.call_tool("start_job", {
                    "command": background_command,
                    "idempotency_key": IDEMPOTENCY_KEY,
                }, meta={"threadId": THREAD_ID})
                try:
                    start_result = _value(raw_start)
                except AssertionError:
                    start_result = {}
                job_id = start_result.get("job_id") or _reserved_job_id(registry_path)
                assert job_id, start_result
                assert (raw_start.isError or start_result.get("error") or
                        start_result.get("execution_state") == "unknown"), (
                    "the shim must drop the successful job_start response"
                )
                dropped = _events(dropped_log)
                assert len(dropped) == 1 and dropped[0]["op"] == "job_start"
                assert dropped[0]["job_id"] == job_id
                assert dropped[0]["persisted"] is True
                assert dropped[0]["response_was_present"] is True
                assert dropped[0]["command"] == background_command
                assert not drop_gate.exists(), "one-shot response-loss gate was not consumed"
                expected_job_dir = Path(binding_a["project_root"]) / ".remote-workbench-state" / "jobs" / job_id
                assert Path(dropped[0]["job_dir"]) == expected_job_dir
                assert (expected_job_dir / "status.json").is_file()
                assert (expected_job_dir / "origin.json").is_file()
                initial_start_event = next(
                    event for event in _events(event_log)
                    if event.get("op") == "job_start" and event.get("job_id") == job_id
                )
                initial_start_pid = initial_start_event["server_pid"]
                assert _registry_rows(registry_path) == rows_before

        assert _reserved_job_id(registry_path) == job_id

        async with stdio_client(params) as (reader, writer):
            async with ClientSession(reader, writer) as client:
                await client.initialize()
                before_resume_events = len(_events(event_log))
                resumed = await _call(client, "workspace_resume")
                assert resumed["binding"]["workspace_id"] == workspace_id
                assert resumed["remote_query_performed"] is False
                assert "last_test" not in resumed and "latest_test_attempt" not in resumed
                assert len(_events(event_log)) == before_resume_events

                bound_b = await _call(client, "workspace_switch", {
                    "host": "b", "cwd": str(source_b),
                })
                assert bound_b["target_id"] != target_a_id

                retry_raw = await client.call_tool("start_job", {
                    "command": background_command,
                    "idempotency_key": IDEMPOTENCY_KEY,
                }, meta={"threadId": THREAD_ID})
                retry = {} if retry_raw.isError else _value(retry_raw)
                assert retry_raw.isError or retry.get("deduplicated") or retry.get("error")
                start_events = [e for e in _events(event_log) if e.get("op") == "job_start"]
                assert len(start_events) == 1

                resolve_event_offset = len(_events(event_log))
                recovered = await _call(client, "resolve_operation", {
                    "idempotency_key": IDEMPOTENCY_KEY,
                })
                assert recovered["job_id"] == job_id
                assert recovered["target"]["target_id"] == target_a_id
                assert recovered["target"]["host"] == "a"
                assert recovered.get("local_job", {}).get("execution_state") == "running", recovered
                assert "test_attempt" not in recovered and "test_observation" not in recovered
                output_id = recovered["local_job"].get("job_output_id")
                assert output_id
                resolve_events = _events(event_log)[resolve_event_offset:]
                assert resolve_events
                assert all(event["target"] == "simulated-a" for event in resolve_events)
                assert all(event["server_pid"] != initial_start_pid for event in resolve_events)
                assert len({event["server_pid"] for event in resolve_events}) == 1

                local_record = await _wait_for_local_record(
                    state_dir, workspace_id, output_id, timeout=12,
                    marker="ORIGINAL_PROJECT_STARTED",
                )
                assert local_record["execution_state"] == "running"
                assert Path(local_record["remote_job_dir"]) == expected_job_dir
                first_page = None
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    first_page = await _call(client, "job_status", {
                        "job_id": job_id, "stdout_cursor": 0, "stderr_cursor": 0,
                        "max_bytes": 256,
                    })
                    if "CURSOR_PREFIX_START" in first_page.get("stdout_tail", ""):
                        break
                    await asyncio.sleep(0.05)
                assert first_page is not None
                assert first_page["execution_state"] == "running", first_page
                assert first_page["job_output_id"] == output_id
                assert first_page["stdout_has_more"] is True
                assert "ORIGINAL_PROJECT_STARTED" in first_page["stdout_tail"]
                assert "CURSOR_PREFIX_START" in first_page["stdout_tail"]
                assert "CURSOR_PREFIX_END" not in first_page["stdout_tail"]
                assert first_page["next_stdout_cursor"] > 0

                waited_while_held = await _call(client, "job_wait", {
                    "job_id": job_id, "wait_seconds": 0.2,
                    "stdout_cursor": first_page["next_stdout_cursor"],
                    "stderr_cursor": first_page["next_stderr_cursor"], "max_bytes": 256,
                })
                assert waited_while_held["job_id"] == job_id
                assert "CURSOR_PREFIX_START" not in waited_while_held.get("stdout_tail", "")
                second_page = await _call(client, "job_status", {
                    "job_id": job_id,
                    "stdout_cursor": first_page["next_stdout_cursor"],
                    "stderr_cursor": first_page["next_stderr_cursor"], "max_bytes": 256,
                })
                assert second_page["job_output_id"] == output_id
                assert second_page["next_stdout_cursor"] > first_page["next_stdout_cursor"]
                assert "CURSOR_PREFIX_START" not in second_page.get("stdout_tail", "")

                count_lines = launch_counter.read_text(encoding="utf-8").splitlines()
                assert count_lines == ["A"], count_lines
                stop_event_offset = len(_events(event_log))
                stopped = await _call(client, "stop_job", {"job_id": job_id})
                assert stopped.get("cancel_requested") is True, stopped
                stop_requested = True
                # stop_job flattens the worker's remote_task fields into its result.
                stopped_remote_task = stopped
                assert stopped_remote_task.get("state") == "finished", stopped_remote_task
                assert stopped_remote_task.get("reason") == "cancelled", stopped_remote_task
                assert stopped_remote_task.get("exit_code") == 130, stopped_remote_task
                assert stopped_remote_task.get("cleanup_state") == "process_group_signalled"
                assert stopped_remote_task.get("tree_confirmed") is False
                stop_terminal_confirmed = True
                stop_cleanup_confirmed = stopped_remote_task.get("tree_confirmed") is True
                assert stopped.get("output_id")
                stop_audit = await _call(client, "audit_get", {
                    "operation_id": stopped["output_id"],
                })
                assert stop_audit["host"] == "simulated-a"
                assert stop_audit["command"] == f"stop jobs/{job_id}"
                stop_call_events = _events(event_log)[stop_event_offset:]
                assert stop_call_events and all(
                    event["target"] == "simulated-a" for event in stop_call_events
                )
                stop_events = [event for event in _events(event_log)
                               if event.get("op") == "cancel_task" and event.get("job_id") == job_id]
                assert stop_events and all(event["target"] == "simulated-a" for event in stop_events)

                release_gate.unlink(missing_ok=True)
                release_gate_removed = True
                waited_after_stop = await _call(client, "job_wait", {
                    "job_id": job_id, "wait_seconds": 8,
                    "stdout_cursor": second_page["next_stdout_cursor"],
                    "stderr_cursor": second_page["next_stderr_cursor"], "max_bytes": 1024,
                })
                assert waited_after_stop["job_id"] == job_id
                assert waited_after_stop["job_output_id"] == output_id
                assert "test_attempt" not in waited_after_stop
                terminal_wait_state = waited_after_stop.get("execution_state")
                terminal_states = {"finished", "failed", "cancelled", "timed_out"}
                collector_wait_outcome = (
                    "confirmed_terminal" if terminal_wait_state in terminal_states else "progress_or_wait_ended"
                )
                terminal_record = await _wait_for_local_terminal_record(
                    state_dir, workspace_id, output_id, timeout=12,
                )
                assert terminal_record["execution_state"] in terminal_states, terminal_record
                assert terminal_record["exit_code"] == stopped_remote_task["exit_code"]
                final_status = await _call(client, "job_status", {
                    "job_id": job_id,
                    "stdout_cursor": terminal_record.get("stdout_bytes", 0),
                    "stderr_cursor": terminal_record.get("stderr_bytes", 0), "max_bytes": 1024,
                })
                assert final_status["job_output_id"] == output_id
                final_status_state = final_status.get("execution_state")
                assert final_status_state in terminal_states, final_status
                assert final_status.get("sync_state") == "complete", final_status
                assert final_status.get("exit_code") == stopped_remote_task["exit_code"]
                collector_final_outcome = "confirmed_terminal"
                assert "ORIGINAL_PROJECT_STARTED" not in final_status.get("stdout_tail", "")

                output_audit = await _call(client, "audit_get", {"operation_id": output_id})
                assert output_audit["id"] == output_id and output_audit["job_id"] == job_id
                saved_job_stdout = await _call(client, "read_output", {
                    "output_id": output_id, "stream": "stdout",
                })
                saved_job_stderr = await _call(client, "read_output", {
                    "output_id": output_id, "stream": "stderr",
                })
                assert "ORIGINAL_PROJECT_STARTED" in saved_job_stdout["content"]
                assert "test_background_result" in saved_job_stderr["content"]

                assert _registry_rows(registry_path) == rows_before
                fingerprint_events = [event for event in _events(event_log)
                                      if event.get("op") == "fingerprint"]
                assert fingerprint_events == []
                start_events = [event for event in _events(event_log) if event.get("op") == "job_start"]
                assert len(start_events) == 1 and start_events[0]["target"] == "simulated-a"
                job_control_events = [event for event in _events(event_log)
                                      if event.get("op") in {"job_status", "cancel_task", "job_log_follow"}
                                      and event.get("job_id") == job_id]
                assert job_control_events
                assert all(event["target"] == "simulated-a" for event in job_control_events)
                _write_acceptance_evidence(evidence_dir, "phase1-stdio-evidence.json", {
                    "thread_id": THREAD_ID,
                    "workspace_id": workspace_id,
                    "target_a_id": target_a_id,
                    "job_id": job_id,
                    "job_output_id": output_id,
                    "dropped_successful_start_ack": True,
                    "new_mcp_process_pid": sorted({event["server_pid"] for event in resolve_events}),
                    "start_request_count": len(start_events),
                    "launch_count": count_lines,
                    "default_fingerprint_calls": len(fingerprint_events),
                    "legacy_rows_unchanged": True,
                    "stop_requested": stop_requested,
                    "remote_terminal_confirmed": stop_terminal_confirmed,
                    "remote_terminal_reason": stopped_remote_task["reason"],
                    "remote_terminal_exit_code": stopped_remote_task["exit_code"],
                    "remote_cleanup_state": stopped_remote_task["cleanup_state"],
                    "process_tree_confirmed": stopped_remote_task["tree_confirmed"],
                    "collector_terminal_sync_state": terminal_record["sync_state"],
                    "collector_terminal_state": terminal_wait_state,
                    "collector_wait_outcome": collector_wait_outcome,
                    "collector_final_state": final_status_state,
                    "collector_final_outcome": collector_final_outcome,
                    "partial_cursors": [0, first_page["next_stdout_cursor"],
                                        second_page["next_stdout_cursor"]],
                    "control_events_target": sorted({event["target"] for event in job_control_events}),
                    "ssh_events": _events(event_log),
                })
    finally:
        if not release_gate_removed:
            release_gate.unlink(missing_ok=True)
        final_cleanup = None
        if job_id and not stop_cleanup_confirmed:
            try:
                async with stdio_client(params) as (reader, writer):
                    async with ClientSession(reader, writer) as cleanup_client:
                        await cleanup_client.initialize()
                        final_cleanup = await asyncio.wait_for(
                            _call(cleanup_client, "stop_job", {"job_id": job_id}), timeout=10)
            except Exception:
                final_cleanup = {"execution_state": "unknown", "cleanup_attempted": True}
        evidence_path = evidence_dir / "phase1-stdio-evidence.json"
        if evidence_path.is_file():
            evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
            evidence["finally_gate_released"] = not release_gate.exists()
            evidence["finally_cleanup_attempted"] = bool(job_id and not stop_cleanup_confirmed)
            if isinstance(final_cleanup, dict):
                cleanup_task = final_cleanup.get("remote_task")
                if not isinstance(cleanup_task, dict):
                    cleanup_task = final_cleanup
                evidence["finally_cleanup_outcome"] = {
                    key: cleanup_task.get(key) for key in (
                        "execution_state", "state", "reason", "exit_code",
                        "cleanup_state", "tree_confirmed", "cancel_requested",
                    )
                }
            else:
                evidence["finally_cleanup_outcome"] = None
            evidence["ssh_events"] = _events(event_log)
            _write_acceptance_evidence(evidence_dir, "phase1-stdio-evidence.json", evidence)
