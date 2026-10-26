import json
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def test_real_stdio_metadata_and_process_restart(tmp_path):
    config = tmp_path / "hosts.yaml"
    config.write_text('hosts:\n  test:\n    ssh_host: never-connect.invalid\n')
    params = StdioServerParameters(command=sys.executable, args=[
        "-m", "remote_workbench.session_server", "--config", str(config),
        "--data-dir", str(tmp_path / "state"), "--viewer-port", "19869"])

    async def inspect_process():
        async with stdio_client(params) as (reader, writer):
            async with ClientSession(reader, writer) as client:
                await client.initialize()
                missing = await client.call_tool("workspace_info")
                assert missing.isError and "threadId is unavailable" in missing.content[0].text
                first = await client.call_tool("workspace_info", meta={"threadId": "thread-a"})
                second = await client.call_tool("workspace_info", meta={"threadId": "thread-b"})
                a, b = json.loads(first.content[0].text), json.loads(second.content[0].text)
                assert not first.isError and not second.isError
                assert a["workspace_id"] != b["workspace_id"]
                assert not a["bound"] and not b["bound"]
                assert "operation_log" in a
                return a["workspace_id"], b["workspace_id"]

    assert await inspect_process() == await inspect_process()


async def test_identity_only_cli_does_not_load_hosts_or_remote_tools(tmp_path):
    params = StdioServerParameters(command=sys.executable, args=[
        "-m", "remote_workbench.session_server", "--identity-probe", "--config", "/does/not/exist.yaml",
        "--data-dir", str(tmp_path), "--viewer-port", "19869"])
    async with stdio_client(params) as (reader, writer):
        async with ClientSession(reader, writer) as client:
            await client.initialize()
            tools = await client.list_tools()
            assert [tool.name for tool in tools.tools] == ["session_identity_probe"]
            result = await client.call_tool("session_identity_probe", meta={"threadId": "test-client-only"})
            value = json.loads(result.content[0].text)
            assert value["identity_available"] and value["remote_execution"] is False
            assert (tmp_path / "identity-probe" / value["operation_id"] / "record.json").exists()


async def test_compact_stdio_schema_scoping_and_reused_operation_link(tmp_path):
    config = tmp_path / "hosts.yaml"
    config.write_text('hosts:\n  test:\n    ssh_host: never-connect.invalid\n')
    params = StdioServerParameters(command=sys.executable, args=[
        "-m", "remote_workbench.session_server", "--config", str(config),
        "--data-dir", str(tmp_path / "state"), "--viewer-port", "0", "--tool-profile", "compact"])
    async with stdio_client(params) as (reader, writer):
        async with ClientSession(reader, writer) as client:
            await client.initialize()
            tools = await client.list_tools()
            assert len(tools.tools) == 8
            assert "workspace_tools" in {t.name for t in tools.tools}
            first = await client.call_tool("workspace_info", meta={"threadId": "a"})
            assert first.structuredContent is None and len(first.content) == 1
            link = json.loads(first.content[0].text)["operation_log"]
            saved = await client.call_tool("workspace_tools", {"tool": "workspace_note", "arguments": {"goal": "private note"}}, meta={"threadId": "a"})
            assert not saved.isError
            assert "operation_log" not in json.loads(saved.content[0].text)
            resumed = await client.call_tool("workspace_tools", {"tool": "workspace_resume"}, meta={"threadId": "a"})
            a = json.loads(resumed.content[0].text)
            assert a["operation_log"] == link and a["note"]["goal"] == "private note"
            other = await client.call_tool("workspace_tools", {"tool": "workspace_resume"}, meta={"threadId": "b"})
            b = json.loads(other.content[0].text)
            assert b["note"] is None and b["operation_log"] != link
            refused = await client.call_tool("exec_command", {"command": "must never run"}, meta={"threadId": "a"})
            assert refused.isError and "no remote workspace" in refused.content[0].text
