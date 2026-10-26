import asyncio
from pathlib import Path
import json
import subprocess
import sqlite3
from unittest.mock import AsyncMock

import pytest
from mcp import types
from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.lowlevel.server import request_ctx
from mcp.shared.context import RequestContext

from remote_workbench import audit, job_collector
from remote_workbench import test_evidence
from remote_workbench import session_server as session_server_module
from remote_workbench.config import HostConfig, ServerConfig
from remote_workbench.server import require_connection, get_executor, workspace_executor
from remote_workbench.session_server import (
    _public_test_attempt, create_server,
)
from remote_workbench.ssh import SSHExecutor, SSHResult
from remote_workbench.tools import commands, files, git, search, patch, audit as audit_tools
from remote_workbench.workspace_router import SessionRouter
from remote_workbench.workspace_registry import WorkspaceTarget


async def call(server, thread, name, **arguments):
    meta = types.RequestParams.Meta(**({"threadId": thread, "sessionId": "test-transport"} if thread else {}))
    token = request_ctx.set(RequestContext(request_id=1, meta=meta, session=None, lifespan_context=None))
    try:
        blocks = await server.call_tool(name, arguments)
        assert len(blocks) == 1
        return json.loads(blocks[0].text)
    finally:
        request_ctx.reset(token)


def test_legacy_test_attempt_projection_is_historical_and_read_only():
    saved_observation = {
        "attempt_id": "a" * 32, "attempt_order": 4, "test_result": "success",
        "reliable_completion": True, "applies_to_current_code": True,
        "before": {"digest": "same"}, "after": {"digest": "same"},
        "observed_at": 123.0,
    }
    attempt = {
        "attempt_id": "a" * 32, "attempt_order": 4, "state": "finished",
        "process_exit_status": "success", "test_result": "success",
        "before": saved_observation["before"], "after": saved_observation["after"],
        "observation": saved_observation,
    }
    cached = _public_test_attempt(attempt, applicability=None, historical=True)
    assert cached["applies_to_current_code"] is None
    assert cached["historical"] is True
    assert "observation" not in cached
    assert saved_observation["applies_to_current_code"] is True
    assert attempt["observation"] == saved_observation


def router_at(path):
    return SessionRouter(ServerConfig(hosts={"a": HostConfig("host-a", execution_enabled=True), "b": HostConfig("host-b", execution_enabled=True)}), path)


async def test_schema_context_injection_and_missing_identity_fail_closed(tmp_path):
    router = router_at(tmp_path)
    server = create_server(router)
    tools = await server.list_tools()
    assert len(tools) >= 20
    assert "resolve_operation" in {tool.name for tool in tools}
    for tool in tools:
        fields = tool.inputSchema["properties"]
        assert not {"ctx", "thread_id", "threadId", "workspace_id", "session_id"} & fields.keys()
        assert ("host" in fields) == (tool.name == "workspace_switch")
        assert tool.outputSchema is None
    with pytest.raises(ToolError, match="threadId is unavailable"):
        await call(server, None, "exec_command", command="must never run")
    assert not router._executors
    info = await call(server, "a", "workspace_info")
    assert info["bound"] is False and info["mode"] == "local"
    with pytest.raises(ToolError, match="no remote workspace"):
        await call(server, "a", "exec_command", command="must never run")
    assert not router._executors


async def test_local_default_switches_back_to_saved_remote_target_and_keeps_job_origin(project, monkeypatch):
    router, _, source, _ = project
    calls = []

    async def job_status(job_id: str, host: str | None = None):
        calls.append(("status", host, job_id))
        return {"job_id": job_id, "routed_host": host}

    async def stop_job(job_id: str, host: str | None = None):
        calls.append(("stop", host, job_id))
        return {"job_id": job_id, "routed_host": host}

    async def job_wait(job_id: str, host: str | None = None, wait_seconds: float = 10):
        calls.append(("wait", host, job_id))
        return {"job_id": job_id, "routed_host": host}

    replacements = {"job_status": job_status, "stop_job": stop_job, "job_wait": job_wait}
    monkeypatch.setattr(session_server_module, "TOOLS", [
        (name, replacements.get(name, function), description)
        for name, function, description in session_server_module.TOOLS
    ])
    server = create_server(router)

    initial = await call(server, "a", "workspace_info")
    assert initial["mode"] == "local" and initial["bound"] is False
    assert initial["execution"] == "native-local-tools"

    remote = await call(server, "a", "workspace_switch", host="a", cwd=str(source))
    changed = await call(server, "a", "write_file", path="value.txt", content="private edit\n")
    assert changed["success"] is True
    origin = router.registry.find_target(remote["workspace_id"], "a", str(source))
    assert origin is not None
    router.registry.register_job(remote["workspace_id"], "saved-job", origin)

    local = await call(server, "a", "workspace_use_local", expected_revision=remote["revision"])
    assert local["mode"] == "local" and local["bound"] is False
    assert local["revision"] == remote["revision"] + 1
    assert local["host"] is None and local["cwd"] is None
    info = await call(server, "a", "workspace_info")
    resumed = await call(server, "a", "workspace_resume")
    assert info["mode"] == "local" and info["host"] is None
    assert resumed["binding"]["mode"] == "local"
    assert resumed["jobs"][0]["job_id"] == "saved-job"

    # The remote-facing file/command tools must fail before asking the router
    # for any executor when this thread has returned to local mode.
    executor_calls = []
    original_executor = router.executor

    def track_executor(*args, **kwargs):
        executor_calls.append(args)
        return original_executor(*args, **kwargs)

    monkeypatch.setattr(router, "executor", track_executor)
    for name, arguments in (
        ("exec_command", {"command": "pwd"}),
        ("read_file", {"path": "value.txt"}),
        ("workspace_changes", {}),
    ):
        with pytest.raises(ToolError, match="no remote workspace"):
            await call(server, "a", name, **arguments)
    assert executor_calls == []

    # Job operations remain routed to their immutable origin in local mode.
    for name, arguments in (
        ("job_status", {}), ("stop_job", {}), ("job_wait", {"wait_seconds": 0}),
    ):
        result = await call(server, "a", name, job_id="saved-job", **arguments)
        assert result["routed_host"] == "a"
    assert [(kind, host) for kind, host, _ in calls] == [
        ("status", "a"), ("stop", "a"), ("wait", "a")]

    restored = await call(server, "a", "workspace_switch", host="a", cwd=str(source),
                          expected_revision=local["revision"])
    assert restored["cwd"] == remote["cwd"]
    assert Path(restored["cwd"], "value.txt").read_text() == "private edit\n"
    assert restored["revision"] == local["revision"] + 1


async def test_workspace_use_local_is_revision_guarded_and_idempotent(project):
    router, server, source, _ = project
    first = await call(server, "a", "workspace_use_local", expected_revision=0)
    assert first["mode"] == "local" and first["revision"] == 0
    remote = await call(server, "a", "workspace_switch", host="a", cwd=str(source))
    with pytest.raises(ToolError, match="revision changed"):
        await call(server, "a", "workspace_use_local", expected_revision=0)
    assert (await call(server, "a", "workspace_info"))["cwd"] == remote["cwd"]
    current = await call(server, "a", "workspace_use_local", expected_revision=remote["revision"])
    assert current["revision"] == remote["revision"] + 1
    assert router.registry.history(current["workspace_id"])[0]["target_id"] == remote["target_id"]


async def test_history_limit_schema_and_validation(tmp_path):
    router = router_at(tmp_path)
    server = create_server(router)
    tool = next(tool for tool in await server.list_tools() if tool.name == "workspace_history")
    limit = tool.inputSchema["properties"]["limit"]
    assert limit["type"] == "integer"
    assert limit["default"] == 20
    assert (await call(server, "a", "workspace_history", limit=0))["targets"] == []
    with pytest.raises(ToolError, match="non-negative integer"):
        await call(server, "a", "workspace_history", limit=-1)
    assert not router._executors


async def test_workspace_resume_and_note_are_local_and_thread_scoped(project, monkeypatch):
    router, server, source, _ = project
    switched = await call(server, "a", "workspace_switch", host="a", cwd=str(source))
    workspace_id = switched["workspace_id"]
    state = router.registry.get(workspace_id)
    target = WorkspaceTarget(state.target_id, workspace_id, state.host, state.source_cwd,
                             state.cwd, state.project_root)
    router.registry.register_job(workspace_id, "resume-job", target)
    executor = router.executor(state.host, state.cwd, state.project_root)
    monkeypatch.setattr(executor, "job_local_info", lambda job_id: {
        "job_id": job_id, "job_output_id": "b" * 32, "running": False, "exit_code": 0,
        "execution_state": "finished", "sync_state": "complete", "stdout_bytes": 14,
        "stderr_bytes": 0, "synced_at": 123.0,
    }, raising=False)
    monkeypatch.setattr(executor, "check_connection", AsyncMock(side_effect=AssertionError("SSH check")))
    monkeypatch.setattr(executor, "run_program", AsyncMock(side_effect=AssertionError("SSH command")))

    saved = await call(server, "a", "workspace_note", goal="finish the feature",
                       constraints="keep sessions independent", next_step="review the patch")
    assert saved["updated"] is True
    resumed = await call(server, "a", "workspace_resume")
    assert resumed["binding"]["cwd"] == switched["cwd"]
    assert resumed["jobs"][0]["job_id"] == "resume-job"
    assert resumed["jobs"][0]["local_status"]["sync_state"] == "complete"
    assert resumed["jobs"][0]["local_cache_age"] == "unknown"
    assert resumed["remote_query_performed"] is False
    assert resumed["job_collection_restarted"] is False
    assert resumed["note"]["goal"] == "finish the feature"

    other_thread = await call(server, "b", "workspace_resume")
    assert other_thread["binding"]["workspace_id"] != workspace_id
    assert other_thread["jobs"] == []
    assert other_thread["note"] is None


async def test_workspace_resume_bounds_and_pages_local_recovery_details(project, monkeypatch):
    router, server, source, _ = project
    switched = await call(server, "a", "workspace_switch", host="a", cwd=str(source))
    workspace_id = switched["workspace_id"]
    state = router.registry.get(workspace_id)
    target = WorkspaceTarget(state.target_id, workspace_id, state.host, state.source_cwd,
                             state.cwd, state.project_root)
    for index in range(23):
        router.registry.register_job(workspace_id, f"page-{index:02d}", target)
    with audit.scoped_root(router.audit_dir(workspace_id)):
        test_operation_id = audit.create(
            kind="tool", tool="exec_command", host=state.host, cwd=state.cwd,
            status="finished", command="pytest",
        )
        delivery_id = audit.create(
            kind="tool", tool="workspace_preview", host=state.host, cwd=state.cwd,
            status="finished", delivery_result={
                "files": [{"path": f"file-{index:02d}.py", "change": "modified"} for index in range(25)]
            },
        )
    router.registry.save_test(workspace_id, state.target_id, {
        "command": "pytest " + ("--verbose " * 2000),
        "operation_id": test_operation_id,
        "output_id": "pending-output",
        "status": "passed",
        "applies_to_current_code": True,
        "historical": False,
        "before": {
            "code": {"complete": True, "digest": "a" * 64, "scope": "bounded"},
            "config_digest": "b" * 64,
            "observed_at": 1.0,
        },
        "after": {
            "code": {"complete": True, "digest": "a" * 64, "scope": "bounded"},
            "config_digest": "b" * 64,
            "observed_at": 2.0,
        },
    })
    executor = router.executor(state.host, state.cwd, state.project_root)
    monkeypatch.setattr(executor, "job_local_info",
                        lambda job_id: {"job_id": job_id, "running": None}, raising=False)
    monkeypatch.setattr(executor, "check_connection", AsyncMock(side_effect=AssertionError("SSH check")))
    monkeypatch.setattr(executor, "run_program", AsyncMock(side_effect=AssertionError("SSH command")))

    first = await call(server, "a", "workspace_resume")
    assert len(first["jobs"]) == 20
    assert first["jobs_has_more"] is True
    assert first["jobs_next_offset"] == 20
    assert first["jobs_truncated"] is True
    assert "last_test" not in first and "latest_test_attempt" not in first
    legacy = await call(server, "a", "workspace_resume", include_legacy_test_history=True)
    saved_test = legacy["legacy_test_history"]["last_test"]
    assert saved_test["local_record_id"] == test_operation_id
    assert saved_test["applies_to_current_code"] is None
    assert saved_test["historical"] is True
    assert saved_test["current_applicability"] == "unknown"
    assert "command" not in saved_test
    assert len(saved_test["command_preview"]) <= 241
    assert set(saved_test["before"]["code"]) <= {
        "complete", "digest", "scope", "files", "bytes", "reason",
    }
    delivery = next(item for item in first["recent_delivery"] if item["id"] == delivery_id)
    assert len(delivery["files"]) == 20
    assert delivery["files_total"] == 25
    assert delivery["truncated"] is True

    second = await call(server, "a", "workspace_resume", job_offset=first["jobs_next_offset"], limit=50)
    all_ids = [item["job_id"] for item in first["jobs"] + second["jobs"]]
    assert len(all_ids) == 23 and len(set(all_ids)) == 23
    assert second["jobs_has_more"] is False
    assert second["jobs_next_offset"] is None

    other = await call(server, "b", "workspace_resume")
    assert other["jobs"] == []
    assert other["jobs_has_more"] is False
    with pytest.raises(ToolError, match="between 1 and 50"):
        await call(server, "a", "workspace_resume", limit=51)


async def test_default_lifecycle_never_manages_legacy_test_evidence(project, monkeypatch):
    router, server, source, _ = project
    tests_dir = source / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_smoke.py").write_text(
        "import unittest\nclass Smoke(unittest.TestCase):\n"
        "    def test_ok(self): self.assertEqual(1 + 1, 2)\n"
    )
    switched = await call(server, "a", "workspace_switch", host="a", cwd=str(source))
    state = router.registry.get(switched["workspace_id"])
    target = WorkspaceTarget(state.target_id, state.workspace_id, state.host, state.source_cwd,
                             state.cwd, state.project_root)
    router.registry.save_test(state.workspace_id, state.target_id, {
        "command": "pytest -q", "operation_id": "d" * 32, "status": "passed",
        "applies_to_current_code": True, "before": {"code": {"digest": "legacy"}},
    })
    old_attempt = router.registry.begin_test_attempt(
        state.workspace_id, target, attempt_id="e" * 32, job_id="legacy-job",
        command="pytest -q", cwd=state.cwd, execution_kind="foreground",
        started_at=1.0, before={"code": {"digest": "legacy"}}, revision=state.revision,
    )
    saved_test = router.registry.last_test(state.workspace_id, state.target_id)
    saved_attempt = router.registry.test_attempt(state.workspace_id, old_attempt["attempt_id"])
    def forbidden(*args, **kwargs):
        raise AssertionError("normal execution called a legacy test-evidence helper")

    legacy_methods = (
        "last_test", "latest_test_attempt", "test_attempt_for_job", "test_attempt",
        "begin_test_attempt", "update_test_attempt", "save_test",
    )
    with monkeypatch.context() as blocked:
        for method in legacy_methods:
            blocked.setattr(router.registry, method, forbidden)
        for method in ("capture", "capture_target", "compare", "invalidate"):
            blocked.setattr(test_evidence, method, forbidden)
        blocked.setattr(session_server_module.baseline, "project_fingerprint", forbidden)

        tested = await call(server, "a", "exec_command",
                            command="python3 -m unittest discover -s tests")
        assert "Ran 1 test" in tested["stderr"] and "OK" in tested["stderr"]
        assert "test_evidence" not in tested and "test_attempt" not in tested

        resumed = await call(server, "a", "workspace_resume")
        assert "last_test" not in resumed and "latest_test_attempt" not in resumed
        basic_snapshot = await call(server, "a", "workspace_snapshot")
        assert "content_fingerprint" not in basic_snapshot
        assert "last_test" not in basic_snapshot and "test_evidence_comparison" not in basic_snapshot

        launched = await call(server, "a", "start_job",
                              command="python3 -m unittest discover -s tests",
                              idempotency_key="default-test-job")
        assert launched.get("job_output_id") and "test_attempt" not in launched
        resolved = await call(server, "a", "resolve_operation",
                              idempotency_key="default-test-job")
        assert resolved["job_id"] == launched["job_id"]
        assert "test_attempt" not in resolved and "test_observation" not in resolved
        stopped = await call(server, "a", "stop_job", job_id=launched["job_id"])
        assert "test_attempt" not in stopped and "test_observation" not in stopped

        executor = router.executor(state.host, state.cwd, state.project_root)
        with audit.scoped_root(router.audit_dir(state.workspace_id)):
            handle = job_collector._read_handle(
                job_collector._handle_path(executor, launched["job_id"]))
        assert not {"test_attempt_id", "registry_path"} & set(handle.get("origin", {}))

    assert router.registry.last_test(state.workspace_id, state.target_id) == saved_test
    assert router.registry.test_attempt(state.workspace_id, old_attempt["attempt_id"]) == saved_attempt
    history = await call(server, "a", "workspace_resume", include_legacy_test_history=True)
    legacy = history["legacy_test_history"]
    assert legacy["last_test"]["applies_to_current_code"] is None
    assert legacy["latest_test_attempt"]["applies_to_current_code"] is None
    assert legacy["latest_test_attempt"]["historical"] is True
    assert router.registry.last_test(state.workspace_id, state.target_id) == saved_test
    assert router.registry.test_attempt(state.workspace_id, old_attempt["attempt_id"]) == saved_attempt

    fingerprint = AsyncMock(return_value={"complete": True, "digest": "opt-in", "files": 7,
                                         "bytes": 2048, "scope": "bounded"})
    monkeypatch.setattr(session_server_module.baseline, "project_fingerprint", fingerprint)
    scanned = await call(server, "a", "workspace_snapshot", scan_content=True,
                         max_files=17, max_content_bytes=4096)
    assert scanned["content_fingerprint"]["digest"] == "opt-in"
    assert scanned["content_scan_budget"] == {"max_files": 17, "max_content_bytes": 4096}
    assert fingerprint.await_count == 1
    assert fingerprint.await_args.kwargs == {"max_files": 17, "max_bytes": 4096}
    assert router.registry.last_test(state.workspace_id, state.target_id) == saved_test
    assert router.registry.test_attempt(state.workspace_id, old_attempt["attempt_id"]) == saved_attempt


async def test_writes_and_exec_leave_legacy_test_history_untouched(project, monkeypatch):
    router, server, source, _ = project
    subprocess.run(["git", "init", "-q", str(source)], check=True)
    subprocess.run(["git", "-C", str(source), "add", "."], check=True)
    subprocess.run(["git", "-C", str(source), "-c", "user.name=Test", "-c",
                    "user.email=test@example.invalid", "commit", "-qm", "baseline"], check=True)
    switched = await call(server, "a", "workspace_switch", host="a", cwd=str(source))
    workspace_id, target_id = switched["workspace_id"], switched["target_id"]
    saved = {"command": "pytest", "operation_id": "f" * 32, "status": "passed",
             "applies_to_current_code": True}
    router.registry.save_test(workspace_id, target_id, saved)
    def forbidden(*args, **kwargs):
        raise AssertionError("write path called a legacy test-evidence helper")

    with monkeypatch.context() as blocked:
        for method in ("last_test", "latest_test_attempt", "test_attempt_for_job",
                       "test_attempt", "begin_test_attempt", "update_test_attempt", "save_test"):
            blocked.setattr(router.registry, method, forbidden)
        for method in ("capture", "capture_target", "compare", "invalidate"):
            blocked.setattr(test_evidence, method, forbidden)

        written = await call(server, "a", "write_file", path="value.txt", content="written\n")
        assert not written.get("error"), written

        patched = await call(server, "a", "patch_file", path="value.txt", old="written",
                             new="patched")
        assert not patched.get("error"), patched

        applied = await call(server, "a", "apply_patch", cwd=".", patch=(
            "*** Begin Patch\n*** Update File: value.txt\n@@\n-patched\n+applied\n*** End Patch"
        ))
        assert not applied.get("error"), applied

        preview = await call(server, "a", "workspace_preview", paths=["value.txt"])
        delivered = await call(server, "a", "workspace_apply", files=preview["files"])
        assert any(item["status"] == "applied" for item in delivered["files"]), delivered

        executed = await call(server, "a", "exec_command", command=(
            "python3 -c \"from pathlib import Path; Path('value.txt').write_text('exec-write')\""
        ))
        assert executed["execution_state"] == "finished"
        assert executed["exit_code"] == 0

    assert router.registry.last_test(workspace_id, target_id) == saved


async def test_baseline_delivery_tools_list_preview_and_apply(project):
    router, server, source, _ = project
    switched = await call(server, "a", "workspace_switch", host="a", cwd=str(source))
    private_file = Path(switched["cwd"], "value.txt")
    private_file.write_text("changed in private session\n")

    changes = await call(server, "a", "workspace_changes")
    assert [entry["path"] for entry in changes["files"]] == ["value.txt"]
    assert "diff" not in changes["files"][0]

    preview = await call(server, "a", "workspace_preview", paths=["value.txt"])
    assert "-original" in preview["files"][0]["diff"]
    assert "+changed in private session" in preview["files"][0]["diff"]
    assert preview["operation_id"]
    assert preview["diff_output_id"] == preview["operation_id"]

    applied = await call(server, "a", "workspace_apply", files=preview["files"])
    assert applied["files"][0]["status"] == "applied"
    assert (source / "value.txt").read_text() == "changed in private session\n"


async def test_commands_do_not_create_test_history_and_authentication_stays_rejected(project):
    router, server, source, _ = project
    switched = await call(server, "a", "workspace_switch", host="a", cwd=str(source))
    rejected = await call(server, "a", "exec_command", command="pytest -n auto")
    assert rejected["executed"] is False
    assert "test_evidence" not in rejected and "test_attempt" not in rejected
    assert router.registry.last_test(switched["workspace_id"], switched["target_id"]) is None
    assert router.registry.latest_test_attempt(switched["workspace_id"], switched["target_id"]) is None

    secret_marker = "test-secret-marker"
    auth = await call(server, "a", "exec_command",
                      command=f"sshpass -p {secret_marker} ssh host; pytest -q")
    assert auth["executed"] is False
    assert auth["error"] == "authentication_requires_user_terminal"
    assert "test_evidence" not in auth and "test_attempt" not in auth
    assert router.registry.latest_test_attempt(switched["workspace_id"], switched["target_id"]) is None
    record = await call(server, "a", "audit_get", operation_id=auth["operation_id"])
    assert secret_marker not in json.dumps(record)


@pytest.fixture
async def project(local, monkeypatch):
    _, root = local
    for module in (commands, files, git, search, patch):
        monkeypatch.setattr(module, "require_connection", require_connection)
    monkeypatch.setattr(commands, "get_executor", get_executor)
    monkeypatch.setattr(SSHExecutor, "check_connection", AsyncMock(return_value=(True, "connected")))
    source = root / "source"
    source.mkdir()
    (source / "value.txt").write_text("original\n")
    other = root / "other"
    other.mkdir()
    (other / "value.txt").write_text("other\n")
    router = router_at(root / "local-state")
    server = create_server(router, viewer_url="http://127.0.0.1:9999")
    return router, server, source, other


async def test_threads_private_copies_switch_restart_and_guarded_edits(project):
    router, server, source, other = project
    a, b = await asyncio.gather(*(call(server, thread, "workspace_switch", host="a", cwd=str(source)) for thread in ("a", "b")))
    assert a["cwd"] != b["cwd"]
    assert a["workspace_id"] != b["workspace_id"]
    assert a["revision"] == b["revision"] == 1
    assert f"/w/{a['workspace_id']}/?id=" in a["operation_log"]
    read = await call(server, "a", "read_file", path="value.txt")
    changed = await call(server, "a", "patch_file", path="value.txt", old="original", new="edited", expected_sha256=read["sha256"])
    assert not changed.get("error"), changed
    conflict = await call(server, "a", "patch_file", path="value.txt", old="edited", new="stale", expected_sha256=read["sha256"])
    assert conflict.get("error")
    assert (source / "value.txt").read_text() == "original\n"
    assert Path(b["cwd"], "value.txt").read_text() == "original\n"
    moved = await call(server, "a", "workspace_switch", host="b", cwd=str(other), expected_revision=1)
    assert moved["host"] == "b" and moved["revision"] == 2
    assert (await call(server, "b", "workspace_info"))["cwd"] == b["cwd"]
    restarted = create_server(router_at(router.state_dir))
    assert (await call(restarted, "a", "workspace_info"))["cwd"] == moved["cwd"]
    restored = await call(restarted, "a", "workspace_switch", host="a", cwd=str(source))
    assert restored["cwd"] == a["cwd"] and restored["revision"] == 3
    assert Path(restored["cwd"], "value.txt").read_text() == "edited\n"
    assert len((await call(restarted, "a", "workspace_history"))["targets"]) == 3
    limited = (await call(restarted, "a", "workspace_history", limit=2))["targets"]
    assert [target["revision"] for target in limited] == [3, 2]
    assert workspace_executor.get() is None
    own = await call(server, "a", "audit_get", operation_id=changed["operation_id"])
    assert own["thread_id"] == "a" and own["revision"] == 1
    with pytest.raises(ToolError):
        await call(server, "b", "audit_get", operation_id=changed["operation_id"])


async def test_failed_switch_keeps_previous_target(project):
    router, server, source, _ = project
    before = await call(server, "a", "workspace_switch", host="a", cwd=str(source))
    for host, cwd, revision in [("unknown", str(source), 1), ("a", str(source / "missing"), 1), ("a", str(source), 0)]:
        with pytest.raises(ToolError):
            await call(server, "a", "workspace_switch", host=host, cwd=cwd, expected_revision=revision)
        after = await call(server, "a", "workspace_info")
        assert after["cwd"] == before["cwd"] and after["revision"] == 1


async def test_repository_subdirectory_keeps_copy_outside_source_repo(project):
    router, server, source, _ = project
    subdir = source / "src"
    subdir.mkdir()
    (subdir / "module.py").write_text("VALUE = 1\n")
    subprocess.run(["git", "init", "-q", str(source)], check=True)
    subprocess.run(["git", "-C", str(source), "add", "."], check=True)
    subprocess.run(["git", "-C", str(source), "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "baseline"], check=True)
    result = await call(server, "a", "workspace_switch", host="a", cwd=str(subdir))
    assert result["cwd"].endswith("/project/src")
    assert not Path(result["project_root"]).is_relative_to(source)
    assert Path(result["cwd"], "module.py").read_text() == "VALUE = 1\n"


async def test_job_uses_origin_after_switch_and_cannot_cross_thread(project):
    router, server, source, other = project
    original = await call(server, "a", "workspace_switch", host="a", cwd=str(source))
    job = await call(server, "a", "start_job", command="printf origin-job", job_id="same")
    assert not job.get("error"), job
    await call(server, "a", "workspace_switch", host="b", cwd=str(other))
    for _ in range(40):
        status = await call(server, "a", "job_status", job_id="same")
        if not status.get("running"):
            break
        await asyncio.sleep(0.05)
    assert status["exit_code"] == 0 and status["stdout_tail"] == "origin-job"
    origin = router.registry.get_job(original["workspace_id"], "same")
    assert origin.host == "a" and origin.cwd == original["cwd"]
    stopped = await call(server, "a", "stop_job", job_id="same")
    assert stopped["cancel_requested"] is False and stopped["execution_state"] == "finished"
    assert stopped["task_dir"].startswith(original["cwd"] + "/")
    with pytest.raises(ToolError, match="Unknown job"):
        await call(server, "b", "job_status", job_id="same")
    with pytest.raises(ToolError, match="Unknown job"):
        await call(server, "b", "stop_job", job_id="same")


async def test_switch_waits_for_foreground_call_across_router_instances(project):
    router, server, source, other = project
    state = await call(server, "a", "workspace_switch", host="a", cwd=str(source))
    entered, release = asyncio.Event(), asyncio.Event()
    executor = router.snapshot(router.registry.get(state["workspace_id"])).workspace.executor
    original = executor.run
    async def blocked(*args, **kwargs):
        entered.set()
        await release.wait()
        return await original(*args, **kwargs)
    executor.run = blocked
    first = asyncio.create_task(call(server, "a", "exec_command", command="pwd"))
    await asyncio.wait_for(entered.wait(), 2)
    other_server = create_server(router_at(router.state_dir))
    switching = asyncio.create_task(call(other_server, "a", "workspace_switch", host="b", cwd=str(other)))
    await asyncio.sleep(0.1)
    assert not switching.done()
    release.set()
    result, switched = await asyncio.gather(first, switching)
    assert result["stdout"].strip() == state["cwd"]
    assert switched["host"] == "b"


async def test_job_controls_fail_closed_without_saved_identity_and_local_reads_bypass_lease(project, monkeypatch):
    router, _, source, other = project

    # The production tool list is being extended independently; ensure this
    # test exercises the session routing path for both names on every checkout.
    tools = [item for item in session_server_module.TOOLS
             if item[0] not in {"job_wait", "search_output"}]
    tools.extend([
        ("job_wait", commands.ssh_job_wait, "Wait on locally collected job output."),
        ("search_output", audit_tools.ssh_search_output, "Search locally saved output."),
    ])
    monkeypatch.setattr(session_server_module, "TOOLS", tools)
    server = create_server(router, viewer_url="http://127.0.0.1:9999")

    async def inspect(host, cwd):
        return {"cwd": str(Path(cwd).resolve()), "repository_root": None}

    async def prepare(executor, source_cwd, target_id, destination):
        return {"cwd": destination}

    monkeypatch.setattr(router, "inspect_directory", inspect)
    monkeypatch.setattr("remote_workbench.workspace_router.prepare_project", prepare)

    first = await call(server, "a", "workspace_switch", host="a", cwd=str(source))
    workspace_id = first["workspace_id"]
    origin_target = router.registry.find_target(workspace_id, "a", str(source))
    assert origin_target is not None
    router.registry.register_job(workspace_id, "origin-job", origin_target)
    switched = await call(server, "a", "workspace_switch", host="b", cwd=str(other))

    # Seed one local output record that the audit tools must find within this
    # thread's scoped audit root, independent of its current remote target.
    with audit.scoped_root(router.audit_dir(workspace_id)):
        output_id = audit.create(kind="command", tool="fixture", status="finished")
        with audit.open_stream(output_id, "stdout") as stream:
            stream.write(b"prefix searchable-marker suffix\n")

    origin_snapshot = router.snapshot(router.registry.get(workspace_id), origin_target)
    origin_executor = origin_snapshot.workspace.executor
    current_executor = router.snapshot(router.registry.get(workspace_id)).workspace.executor
    routed_targets = []
    original_snapshot = router.snapshot

    def capture_snapshot(state, target=None):
        if target is not None:
            routed_targets.append(target)
        return original_snapshot(state, target)

    monkeypatch.setattr(router, "snapshot", capture_snapshot)
    entered, release = asyncio.Event(), asyncio.Event()

    async def blocked_run(*args, **kwargs):
        entered.set()
        await release.wait()
        return SSHResult(stdout="foreground complete", stderr="", exit_code=0)

    cancel_calls = []

    async def cancel_job(job_id):
        cancel_calls.append(job_id)
        return {"job_id": job_id, "cancel_requested": True, "origin_host": "host-a"}

    def local_job_info(executor):
        return lambda job_id: {
            "job_output_id": output_id, "running": True, "execution_state": "running",
            "sync_state": "streaming", "remote_task": {"origin_host": executor.config.ssh_host},
        }

    monkeypatch.setattr(current_executor, "run", blocked_run)
    monkeypatch.setattr(origin_executor, "cancel_task", cancel_job)
    monkeypatch.setattr(origin_executor, "job_local_info", local_job_info(origin_executor), raising=False)
    monkeypatch.setattr(current_executor, "job_local_info", local_job_info(current_executor), raising=False)

    foreground = asyncio.create_task(call(server, "a", "exec_command", command="printf hold"))
    await asyncio.wait_for(entered.wait(), 2)
    try:
        controls = await asyncio.wait_for(asyncio.gather(
            call(server, "a", "stop_job", job_id="origin-job"),
            call(server, "a", "job_status", job_id="origin-job"),
            call(server, "a", "job_wait", job_id="origin-job", wait_seconds=0),
            call(server, "a", "audit_list", limit=10),
            call(server, "a", "audit_get", operation_id=output_id),
            call(server, "a", "read_output", output_id=output_id, max_bytes=256),
            call(server, "a", "search_output", output_id=output_id,
                 query="searchable-marker"),
            call(server, "a", "workspace_info"),
        ), 2)
    finally:
        release.set()
    completed = await asyncio.wait_for(foreground, 2)

    stopped, status, waited, listed, record, output, matches, info = controls
    # A registry-only legacy job has no saved process identity. Session job
    # controls must refuse it instead of trusting the currently configured
    # endpoint or an incomplete local lookup.
    for result in (stopped, status, waited):
        assert result["execution_state"] == "unknown"
        assert result["state_reason"]
    assert cancel_calls == []
    assert origin_target in routed_targets
    assert any(item.get("id") == output_id for item in listed["records"])
    assert record["tool"] == "fixture"
    assert output["content"] == "prefix searchable-marker suffix\n"
    assert matches["matches"][0]["context"].find("searchable-marker") >= 0
    assert info["cwd"] == switched["cwd"]
    assert completed["stdout"] == "foreground complete"


async def test_start_job_idempotency_key_recovers_handle_without_resubmitting(project, monkeypatch):
    router, _, source, other = project
    async def inspect(host, cwd):
        return {"cwd": str(Path(cwd).resolve()), "repository_root": None}

    async def prepare(executor, source_cwd, target_id, destination):
        return {"cwd": destination}

    monkeypatch.setattr(router, "inspect_directory", inspect)
    monkeypatch.setattr("remote_workbench.workspace_router.prepare_project", prepare)
    server = create_server(router, viewer_url="http://127.0.0.1:9999")
    initial = await call(server, "a", "workspace_switch", host="a", cwd=str(source))
    submissions = []

    async def fake_edit(executor, payload, **kwargs):
        submissions.append((executor.config.ssh_target, dict(payload)))
        return {"job_id": payload["job_id"], "job_dir": "/simulated/job",
                "remote_task": {"state": "running"}}

    monkeypatch.setattr(commands, "edit", fake_edit)
    first = await call(server, "a", "start_job", command="printf once",
                       idempotency_key="request-7")
    repeated = await call(server, "a", "start_job", command="printf once",
                          idempotency_key="request-7")

    assert len(submissions) == 1
    assert submissions[0][1]["job_id"] == first["job_id"] == repeated["job_id"]
    assert repeated["deduplicated"] is True
    assert first["operation_state"] == "submitted"

    recovered = await call(server, "a", "resolve_operation", idempotency_key="request-7")
    assert recovered["job_id"] == first["job_id"]
    assert recovered["operation_state"] == "submitted"
    assert recovered["target"]["host"] == "a"
    assert recovered["execution_state"] == "unknown"

    with pytest.raises(ToolError, match="different job parameters"):
        await call(server, "a", "start_job", command="printf twice",
                   idempotency_key="request-7")
    await call(server, "a", "workspace_switch", host="b", cwd=str(other))
    with pytest.raises(ToolError, match="different job parameters"):
        await call(server, "a", "start_job", command="printf once",
                   idempotency_key="request-7")
    assert len(submissions) == 1
    assert router.registry.get_job(initial["workspace_id"], first["job_id"]).host == "a"


async def test_unverified_start_actual_cwd_is_not_persisted(project, monkeypatch):
    router, _, source, _ = project

    async def inspect(host, cwd):
        return {"cwd": str(Path(cwd).resolve()), "repository_root": None}

    async def prepare(executor, source_cwd, target_id, destination):
        return {"cwd": destination}

    monkeypatch.setattr(router, "inspect_directory", inspect)
    monkeypatch.setattr("remote_workbench.workspace_router.prepare_project", prepare)
    server = create_server(router, viewer_url="http://127.0.0.1:9999")
    state = await call(server, "a", "workspace_switch", host="a", cwd=str(source))

    async def mismatched_identity(executor, payload, **kwargs):
        return {
            "job_id": payload["job_id"],
            "job_dir": "/untrusted/state/jobs/" + payload["job_id"],
            "actual_cwd": "/untrusted/physical/cwd",
            "remote_task": {"actual_cwd": "/untrusted/physical/cwd",
                            "origin_nonce": "0" * 32},
            "execution_state": "unknown",
            "error": "job_identity_unconfirmed",
            "state_reason": "origin_nonce_mismatch",
        }

    monkeypatch.setattr(commands, "edit", mismatched_identity)
    result = await call(server, "a", "start_job", command="printf no-confirmation",
                        idempotency_key="unverified-cwd")
    saved = router.registry.get_job_origin(state["workspace_id"], result["job_id"])
    operation = router.registry.get_job_operation(state["workspace_id"], "unverified-cwd")

    assert result["error"] == "job_identity_unconfirmed"
    assert saved["actual_cwd"] is None
    assert operation.actual_cwd is None


async def test_rejected_compute_review_can_be_resubmitted_with_the_same_idempotency_key(project, monkeypatch):
    router, _, source, _ = project
    async def inspect(host, cwd):
        return {"cwd": str(Path(cwd).resolve()), "repository_root": None}

    async def prepare(executor, source_cwd, target_id, destination):
        return {"cwd": destination}

    monkeypatch.setattr(router, "inspect_directory", inspect)
    monkeypatch.setattr("remote_workbench.workspace_router.prepare_project", prepare)
    server = create_server(router)
    state = await call(server, "a", "workspace_switch", host="a", cwd=str(source))
    submissions = []

    async def fake_edit(executor, payload, **kwargs):
        submissions.append(dict(payload))
        return {"job_id": payload["job_id"]}

    monkeypatch.setattr(commands, "edit", fake_edit)
    blocked = await call(server, "a", "start_job", command="pytest -n auto",
                         idempotency_key="review-1")
    assert blocked["executed"] is False
    assert blocked["operation_state"] == "rejected"
    assert submissions == []

    accepted = await call(server, "a", "start_job", command="pytest -n auto",
                          idempotency_key="review-1",
                          compute_review={"concurrency": "4 workers", "environment": "local simulation",
                                          "confirmation": "approved for this bounded test"})
    assert accepted["operation_state"] == "submitted"
    assert accepted["job_id"] == blocked["job_id"]
    assert len(submissions) == 1
    assert router.registry.get_job(state["workspace_id"], accepted["job_id"]).host == "a"


async def test_ambiguous_start_failure_is_not_retried_under_the_same_key(project, monkeypatch):
    router, _, source, _ = project
    async def inspect(host, cwd):
        return {"cwd": str(Path(cwd).resolve()), "repository_root": None}

    async def prepare(executor, source_cwd, target_id, destination):
        return {"cwd": destination}

    monkeypatch.setattr(router, "inspect_directory", inspect)
    monkeypatch.setattr("remote_workbench.workspace_router.prepare_project", prepare)
    server = create_server(router)
    state = await call(server, "a", "workspace_switch", host="a", cwd=str(source))
    attempts = []

    async def lose_response(executor, payload, **kwargs):
        attempts.append(dict(payload))
        raise TimeoutError("simulated response loss after possible submission")

    monkeypatch.setattr(commands, "edit", lose_response)
    with pytest.raises(ToolError, match="simulated response loss"):
        await call(server, "a", "start_job", command="printf possibly-started",
                   idempotency_key="ambiguous-1")
    operation = router.registry.get_job_operation(state["workspace_id"], "ambiguous-1")
    assert operation.state == "unknown"

    retry = await call(server, "a", "start_job", command="printf possibly-started",
                       idempotency_key="ambiguous-1")
    assert len(attempts) == 1
    assert retry["job_id"] == operation.job_id
    assert retry["deduplicated"] is True
    assert retry["operation_state"] == "unknown"

    recovered = await call(server, "a", "resolve_operation", idempotency_key="ambiguous-1")
    assert recovered["job_id"] == operation.job_id
    assert recovered["operation_state"] == "unknown"
    assert "Do not restart" in recovered["warning"]


async def test_snapshot_metadata_and_response_byte_accounting(project):
    router, server, source, other = project
    (source / ".gitignore").write_text("__pycache__/\n.remote-workbench-state/\n")
    (source / "tests").mkdir()
    (source / "tests/test_one.py").write_text("import unittest\nclass Check(unittest.TestCase):\n def test_one(self): self.assertEqual(1+1,2)\n")
    subprocess.run(["git", "init", "-q", str(source)], check=True)
    subprocess.run(["git", "-C", str(source), "add", "."], check=True)
    subprocess.run(["git", "-C", str(source), "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "baseline"], check=True)
    state = await call(server, "a", "workspace_switch", host="a", cwd=str(source))
    workspace_id = state["workspace_id"]
    before = router.registry.metrics(workspace_id)
    output = await call(server, "a", "exec_command", command="printf '你好'")
    after = router.registry.metrics(workspace_id)
    expected = len(json.dumps(output, ensure_ascii=False, separators=(",", ":")).encode())
    assert after["result_text_bytes"] - before["result_text_bytes"] == expected
    assert after["recorded_responses"] - before["recorded_responses"] == 1
    await call(server, "a", "read_output", output_id=output["output_id"], max_bytes=256)
    await call(server, "a", "read_output", output_id=output["output_id"], max_bytes=256)
    assert router.registry.metrics(workspace_id)["repeated_output_reads"] == 1
    with audit.scoped_root(router.audit_dir(workspace_id)):
        with (audit.directory(output["output_id"]) / "stdout").open("ab") as stream:
            stream.write(b"new")  # Simulate a growing saved output stream.
    await call(server, "a", "read_output", output_id=output["output_id"], max_bytes=256)
    assert router.registry.metrics(workspace_id)["repeated_output_reads"] == 1
    tested = await call(server, "a", "exec_command", command="python3 -m unittest discover -s tests -v")
    assert tested["exit_code"] == 0
    assert "test_evidence" not in tested and "test_attempt" not in tested
    # Simulate an external editor changing the private project between tools.
    Path(state["cwd"], "value.txt").write_text("changed outside Remote Workbench\n")
    snapshot = await call(server, "a", "workspace_snapshot")
    assert snapshot["git"]["modified_files"] == 1 and snapshot["git"]["status_complete"]
    assert "content_fingerprint" not in snapshot
    assert "last_test" not in snapshot and "test_evidence_comparison" not in snapshot
    assert snapshot["communication"]["recorded_responses"] > 0
    await call(server, "a", "workspace_switch", host="b", cwd=str(other))
    switched_snapshot = await call(server, "a", "workspace_snapshot")
    assert "last_test" not in switched_snapshot and "content_fingerprint" not in switched_snapshot
    restarted = create_server(router_at(router.state_dir))
    await call(restarted, "a", "workspace_switch", host="a", cwd=str(source))
    restarted_snapshot = await call(restarted, "a", "workspace_snapshot")
    assert "last_test" not in restarted_snapshot and "content_fingerprint" not in restarted_snapshot


async def test_accounting_failure_does_not_turn_a_completed_write_into_failure(project, monkeypatch):
    router, server, source, _ = project
    state = await call(server, "a", "workspace_switch", host="a", cwd=str(source))
    def broken(*args, **kwargs):
        raise sqlite3.OperationalError("accounting unavailable")
    monkeypatch.setattr(router.registry, "record_call", broken)
    result = await call(server, "a", "write_file", path="value.txt", content="written once\n")
    assert result.get("success")
    assert Path(state["cwd"], "value.txt").read_text() == "written once\n"


async def test_changes_pages_compact_paths_and_preview_keeps_review_guards(project, monkeypatch):
    router, server, source, _ = project
    await call(server, 'a', 'workspace_switch', host='a', cwd=str(source))
    full = [{'path': f'file-{index:02d}.py', 'change': 'modified', 'supported': True,
             'source_conflict': False, 'guard': {'source': str(index)},
             'source_state': {'large': 'technical metadata'}, 'session_state': {}, 'baseline_state': {}}
            for index in range(23)]
    async def preview(executor, project_root, paths=None, **kwargs):
        selected = full if paths is None else [item for item in full if item['path'] in paths]
        return {'files': [dict(item) for item in selected]}
    monkeypatch.setattr(session_server_module.baseline, 'preview_delivery', preview)
    first = await call(server, 'a', 'workspace_changes')
    assert len(first['files']) == 20 and first['files_total'] == 23
    assert first['next_offset'] == 20 and first['truncated'] is True
    assert 'guard' not in first['files'][0] and 'source_state' not in first['files'][0]
    last = await call(server, 'a', 'workspace_changes', offset=first['next_offset'])
    assert len(last['files']) == 3 and last['next_offset'] is None
    reviewed = await call(server, 'a', 'workspace_preview', paths=['file-00.py'])
    assert reviewed['files'][0]['guard'] == {'source': '0'}
    assert 'source_state' not in reviewed['files'][0]
    with pytest.raises(ToolError, match='20 selected paths'):
        await call(server, 'a', 'workspace_preview', paths=[item['path'] for item in full])
