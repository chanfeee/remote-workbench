import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
import uuid

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from remote_workbench import audit, workspace_session
from remote_workbench.config import HostConfig
from remote_workbench.ssh import SSHExecutor, SSHResult
from remote_workbench.workspace import Workspace, _workspace_response, create_server


def fake_session(audit_dir, *, session_id=None):
    return SimpleNamespace(
        id=session_id or uuid.uuid4().hex,
        audit_dir=audit_dir,
        ensure_ready=AsyncMock(),
    )


def logged_tool(*, result_field="operation_id", create_record=True, response=None):
    async def tool():
        value = dict(response or {})
        if create_record:
            record_id = audit.create(kind="tool", tool="session_test", status="finished")
            token = audit.operation_id.set(record_id)
            try:
                value[result_field] = record_id
            finally:
                audit.operation_id.reset(token)
        return value

    return tool


async def call_response(tool, session, viewer_url="http://127.0.0.1:8768"):
    wrapped = _workspace_response(tool, viewer_url=viewer_url, session=session)
    return json.loads(await wrapped())


@pytest.mark.parametrize("result_field", ["operation_id", "output_id", "id"])
async def test_response_adds_session_id_and_record_deep_link(tmp_path, result_field):
    session = fake_session(tmp_path / "sessions" / "one")
    viewer_url = "http://127.0.0.1:8768/session-one"

    result = await call_response(logged_tool(result_field=result_field), session, viewer_url)

    record_id = result[result_field]
    assert result["session_id"] == session.id
    assert result["operation_log"] == (
        f"[**Remote Workbench 操作记录**]({viewer_url}?id={record_id})"
    )
    assert (session.audit_dir / record_id / "record.json").is_file()
    session.ensure_ready.assert_awaited_once_with()


async def test_response_without_saved_record_links_to_session_overview(tmp_path):
    session = fake_session(tmp_path / "sessions" / "one")
    viewer_url = "http://127.0.0.1:8768/session-one"

    result = await call_response(
        logged_tool(create_record=False, response={"output_id": "a" * 32, "success": True}),
        session,
        viewer_url,
    )

    assert result["session_id"] == session.id
    assert result["operation_log"] == f"[**Remote Workbench 操作记录**]({viewer_url})"
    assert not (session.audit_dir / ("a" * 32) / "record.json").exists()


async def test_error_result_still_links_to_its_saved_operation(tmp_path):
    session = fake_session(tmp_path / "sessions" / "one")
    viewer_url = "http://127.0.0.1:8768/session-one"

    result = await call_response(
        logged_tool(response={"error": "operation failed"}), session, viewer_url
    )

    assert result["error"] == "operation failed"
    assert result["session_id"] == session.id
    assert result["operation_log"].endswith(f"?id={result['operation_id']})")


async def test_exception_becomes_tool_error_with_session_overview_link(tmp_path):
    session = fake_session(tmp_path / "sessions" / "one")
    viewer_url = "http://127.0.0.1:8768/session-one"

    async def broken_tool():
        raise RuntimeError("connection failed")

    wrapped = _workspace_response(broken_tool, viewer_url=viewer_url, session=session)
    with pytest.raises(ToolError) as caught:
        await wrapped()

    assert "connection failed" in str(caught.value)
    assert f"[**Remote Workbench 操作记录**]({viewer_url})" in str(caught.value)
    session.ensure_ready.assert_awaited_once_with()


async def test_concurrent_session_responses_cannot_see_each_others_records(tmp_path):
    sessions = [fake_session(tmp_path / "sessions" / name, session_id=name * 32)
                for name in ("a", "b")]
    labels = ("alpha", "beta")
    record_ids = {}
    both_created = asyncio.Event()

    def make_tool(label):
        async def tool():
            record_id = audit.create(kind="tool", tool=label, status="finished")
            record_ids[label] = record_id
            if len(record_ids) == 2:
                both_created.set()
            await asyncio.wait_for(both_created.wait(), timeout=2)
            other = labels[1] if label == labels[0] else labels[0]
            assert not audit.directory(record_ids[other]).exists()
            return {"operation_id": record_id}

        return tool

    results = await asyncio.gather(*(
        call_response(make_tool(label), session, f"http://127.0.0.1:{8768 + index}")
        for index, (label, session) in enumerate(zip(labels, sessions))
    ))

    assert [result["session_id"] for result in results] == [session.id for session in sessions]
    assert [result["operation_log"].split("?id=")[1][:-1] for result in results] == [
        record_ids[label] for label in labels
    ]
    for index, session in enumerate(sessions):
        own = record_ids[labels[index]]
        foreign = record_ids[labels[1 - index]]
        assert (session.audit_dir / own / "record.json").is_file()
        assert not (session.audit_dir / foreign).exists()


async def test_audit_read_tool_can_skip_session_preparation(tmp_path):
    session = fake_session(tmp_path / "sessions" / "one")

    async def audit_read():
        record_id = audit.create(kind="tool", tool="audit_get", status="finished")
        return {"operation_id": record_id}

    wrapped = _workspace_response(
        audit_read,
        viewer_url="http://127.0.0.1:8768/session-one",
        session=session,
        prepare_session=False,
    )
    result = json.loads(await wrapped())

    assert result["session_id"] == session.id
    session.ensure_ready.assert_not_awaited()
    assert (session.audit_dir / result["operation_id"] / "record.json").is_file()


def make_workspace():
    executor = SSHExecutor(HostConfig(ssh_host="example.invalid"))
    return Workspace("template", "remote", "/remote/projects/template", executor)


def test_workspace_sessions_share_code_but_keep_unique_audit_directories(tmp_path, monkeypatch):
    monkeypatch.setenv("REMOTE_WORKBENCH_AUDIT_DIR", str(tmp_path / "audit"))
    template = make_workspace()

    first = workspace_session.WorkspaceSession(template)
    second = workspace_session.WorkspaceSession(template)

    assert first.id != second.id
    assert first.workspace.cwd == second.workspace.cwd == template.cwd
    assert first.audit_dir != second.audit_dir
    assert first.audit_dir == tmp_path / "audit" / "sessions" / first.id
    assert second.audit_dir == tmp_path / "audit" / "sessions" / second.id


async def test_remote_probe_is_opt_in_and_does_not_activate_another_session(tmp_path, monkeypatch):
    monkeypatch.setenv("REMOTE_WORKBENCH_AUDIT_DIR", str(tmp_path / "audit"))
    template = make_workspace()
    sessions = [workspace_session.WorkspaceSession(template) for _ in range(2)]
    connection_check = AsyncMock(return_value=(True, "connected"))
    remote_probe = AsyncMock(return_value=SSHResult(
        stdout='{"cwd":"/remote/project","shell":"/bin/bash"}', stderr="", exit_code=0,
        output_id="probe-output",
    ))
    prepare = AsyncMock(return_value={"cwd":template.cwd,"home":"/remote/home"})
    monkeypatch.setattr(SSHExecutor, "check_connection", connection_check)
    monkeypatch.setattr(SSHExecutor, "run_program", remote_probe)
    monkeypatch.setattr("remote_workbench.remote_directory.inspect_directory", prepare)
    servers = [create_server(template, session=session) for session in sessions]

    # Tool discovery and default info leave both session copies untouched.
    for server in servers:
        assert await server.list_tools()
        info = json.loads((await server.call_tool("workspace_info", {}))[0].text)
        assert info["probe_performed"] is False
        assert info["connected"] is None
    connection_check.assert_not_awaited()
    remote_probe.assert_not_awaited()
    prepare.assert_not_awaited()
    assert [session.prepared for session in sessions] == [False, False]

    # Explicitly probing the first thread cannot select or prepare the second.
    first = json.loads((await servers[0].call_tool("workspace_info", {"probe": True}))[0].text)
    second = json.loads((await servers[1].call_tool("workspace_info", {}))[0].text)
    assert first["probe_performed"] is True
    assert first["session_prepared"] is True
    assert second["probe_performed"] is False
    assert second["session_prepared"] is False
    assert sessions[0].workspace.cwd == sessions[1].workspace.cwd
    assert sessions[0].prepared is True
    assert sessions[1].prepared is False
    assert connection_check.await_count == 1  # Explicit probe; validation is mocked.
    remote_probe.assert_awaited_once()
    prepare.assert_awaited_once()


async def test_concurrent_ensure_ready_validates_original_directory_once(tmp_path, monkeypatch):
    monkeypatch.setenv("REMOTE_WORKBENCH_AUDIT_DIR", str(tmp_path / "audit"))
    template = make_workspace()
    template.executor.check_connection = AsyncMock(return_value=(True, "connected"))
    session = workspace_session.WorkspaceSession(template)

    async def prepare(*args):
        await asyncio.sleep(0.01)
        return {"cwd":template.cwd,"home":"/remote/home"}

    prepare_project = AsyncMock(side_effect=prepare)
    monkeypatch.setattr("remote_workbench.remote_directory.inspect_directory", prepare_project)

    await asyncio.gather(session.ensure_ready(), session.ensure_ready(), session.ensure_ready())

    template.executor.check_connection.assert_not_awaited()
    prepare_project.assert_awaited_once_with(
        template.executor, template.cwd
    )
    assert session.prepared is True
    saved = json.loads((session.audit_dir / "session.json").read_text())
    assert saved["prepared"] is True
