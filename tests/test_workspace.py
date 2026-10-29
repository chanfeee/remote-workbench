import asyncio
import json
from unittest.mock import AsyncMock

import pytest
import yaml

from remote_workbench.config import HostConfig
from remote_workbench.server import get_executor, require_connection, workspace_executor
from remote_workbench.ssh import SSHExecutor, SSHResult
from remote_workbench.tools import commands, files, git, search, patch
from remote_workbench.workspace import Workspace, bind_tool, create_server


def manifest(tmp_path, **overrides):
    config = tmp_path / "hosts.yaml"
    config.write_text(yaml.safe_dump({"hosts": {"remote": {
        "ssh_host": "server", "execution_enabled": True, "preamble": "source old-env/bin/activate"
    }}}))
    data = {"version": 1, "name": "test", "host": "remote", "cwd": "/remote/project", "config": "hosts.yaml"}
    data.update(overrides)
    path = tmp_path / "workspace.json"
    path.write_text(json.dumps(data))
    return path


def test_manifest_fixes_host_and_cwd_without_environment_reuse(tmp_path):
    workspace = Workspace.load(manifest(tmp_path))
    assert workspace.executor.config.ssh_host == "server"
    assert workspace.executor.config.default_cwd == "/remote/project"
    assert "old-env" not in workspace.executor.config.preamble
    assert workspace.resolve_cwd("src/../tests") == "/remote/project/tests"
    assert workspace.resolve_cwd("/other") == "/other"


@pytest.mark.parametrize("fields", [{"cwd": "relative"}, {"version": 2}, {"name": "bad name"}, {"unknown": True}])
def test_manifest_rejects_invalid_bindings(tmp_path, fields):
    with pytest.raises(ValueError):
        Workspace.load(manifest(tmp_path, **fields))


async def test_public_schemas_have_no_host_and_optional_cwd(tmp_path):
    workspace = Workspace.load(manifest(tmp_path))
    tools = await create_server(workspace).list_tools()
    assert len(tools) == 20
    schemas = {tool.name: tool.inputSchema for tool in tools}
    assert all("host" not in schema["properties"] for schema in schemas.values())
    assert all(tool.outputSchema is None for tool in tools)
    assert "cwd" not in schemas["apply_patch"].get("required", [])
    assert schemas["exec_command"]["properties"]["cwd"]["default"] == "."
    assert schemas["exec_command"]["properties"]["max_bytes"]["default"] == 16384
    assert schemas["read_file"]["properties"]["max_bytes"]["default"] == 4096
    assert schemas["patch_file"]["properties"]["max_bytes"]["default"] == 4096
    assert schemas["git_diff"]["properties"]["max_bytes"]["default"] == 4096
    assert "download" not in schemas


async def test_concurrent_workspaces_cannot_change_each_others_executor(tmp_path):
    a = Workspace("a", "one", "/remote/a", SSHExecutor(HostConfig(ssh_host="one")))
    b = Workspace("b", "two", "/remote/b", SSHExecutor(HostConfig(ssh_host="two")))
    async def probe(cwd=None, host=None):
        await asyncio.sleep(0.01)
        return {"host": get_executor(host).config.ssh_host, "cwd": cwd}
    first = bind_tool(a, "probe", probe, "probe")
    second = bind_tool(b, "probe", probe, "probe")
    assert await asyncio.gather(first(cwd="src"), second()) == [
        {"host": "one", "cwd": "/remote/a/src"}, {"host": "two", "cwd": "/remote/b"}]
    assert workspace_executor.get() is None
    with pytest.raises(TypeError):
        await first(host="two")


async def test_bound_edit_command_and_patch_use_same_root(local, monkeypatch):
    executor, root = local
    executor.check_connection = AsyncMock(return_value=(True, "OK"))
    # Restore production dispatch so this test exercises the workspace binding.
    for module in (commands, files, git, search, patch):
        monkeypatch.setattr(module, "require_connection", require_connection)
    workspace = Workspace("local-test", "remote", str(root), executor)
    server = create_server(workspace)
    async def call(name, **kw):
        result = await server.call_tool(name, kw)
        assert len(result) == 1
        return json.loads(result[0].text)
    created = await call("write_file", path="example.txt", content="before\n")
    assert created.get("success"), created
    await call("exec_command", command="git init -q && git add example.txt")
    changed = await call("apply_patch", patch="--- a/example.txt\n+++ b/example.txt\n@@ -1 +1 @@\n-before\n+after\n")
    assert not changed.get("error"), changed
    assert (root / "example.txt").read_text() == "after\n"
    read = await call("read_file", path="example.txt")
    assert "after" in str(read)
    (root / "child").mkdir()
    command = await call("exec_command", command="pwd", cwd="child")
    assert command["stdout"].strip() == str(root / "child")
    result = await call("exec_command", command="pwd")
    assert result["stdout"].strip() == str(root)
    assert workspace_executor.get() is None


async def test_no_local_execution_fallback_when_disconnected(tmp_path):
    workspace = Workspace.load(manifest(tmp_path))
    workspace.executor.check_connection = AsyncMock(return_value=(False, "Disconnected"))
    workspace.executor.run = AsyncMock()
    invoke = bind_tool(workspace, "exec_command", commands.ssh_run, "run")
    with pytest.raises(ValueError, match="Disconnected"):
        await invoke(command="touch must-not-exist")
    workspace.executor.run.assert_not_awaited()
    assert workspace_executor.get() is None


async def test_workspace_info_is_local_metadata_unless_probe_is_requested(tmp_path):
    workspace = Workspace("test", "remote", "/remote/project", SSHExecutor(HostConfig(ssh_host="server")))
    workspace.executor.check_connection = AsyncMock(side_effect=AssertionError("unexpected SSH check"))
    workspace.executor.run_program = AsyncMock(side_effect=AssertionError("unexpected SSH command"))
    server = create_server(workspace)

    # MCP discovery and default metadata inspection are entirely local.
    assert await server.list_tools()
    result = json.loads((await server.call_tool("workspace_info", {}))[0].text)
    assert result["host"] == "remote"
    assert result["cwd"] == "/remote/project"
    assert result["probe_performed"] is False
    assert result["connected"] is None
    workspace.executor.check_connection.assert_not_awaited()
    workspace.executor.run_program.assert_not_awaited()

    # A probe only touches mocked local methods when explicitly requested.
    workspace.executor.check_connection = AsyncMock(return_value=(True, "connected"))
    workspace.executor.run_program = AsyncMock(return_value=SSHResult(
        stdout='{"cwd":"/remote/project","shell":"/bin/bash"}', stderr="", exit_code=0,
        output_id="probe-output",
    ))
    result = json.loads((await server.call_tool("workspace_info", {"probe": True}))[0].text)
    assert result["probe_performed"] is True
    assert result["connected"] is True
    assert result["ready"] is True
    workspace.executor.check_connection.assert_awaited_once_with(force=True)
    workspace.executor.run_program.assert_awaited_once()
