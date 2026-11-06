import json

from mcp import types
from mcp.server.lowlevel.server import request_ctx
from mcp.shared.context import RequestContext

from remote_workbench.session_server import create_identity_probe


async def test_probe_exposes_no_execution_and_records_only_identity_fields(tmp_path):
    server = create_identity_probe(viewer_url="http://127.0.0.1:8771", audit_dir=tmp_path)
    tools = await server.list_tools()
    assert [t.name for t in tools] == ["session_identity_probe"]
    assert tools[0].inputSchema["properties"] == {}
    for meta, expected in [({}, False), ({"threadId": "actual-client-thread", "sessionId": "diagnostic-session", "secret": "must-not-log"}, True)]:
        token = request_ctx.set(RequestContext(request_id=1, meta=types.RequestParams.Meta(**meta), session=None, lifespan_context=None))
        try:
            blocks = await server.call_tool("session_identity_probe", {})
        finally:
            request_ctx.reset(token)
        result = json.loads(blocks[0].text)
        assert result["identity_available"] is expected
        assert result["remote_execution"] is False
        record = (tmp_path / result["operation_id"] / "record.json").read_text()
        assert "must-not-log" not in record and "must-not-log" not in blocks[0].text
        assert 'operation_log' not in result
