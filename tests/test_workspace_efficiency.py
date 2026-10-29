import errno
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from threading import Thread
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.error import HTTPError
from urllib.request import urlopen

from mcp.types import CallToolResult, TextContent
import pytest

from remote_workbench.ssh import SSHExecutor
from remote_workbench import workspace as workspace_module
from remote_workbench.workspace import Workspace, create_server


def decode_content(blocks):
    assert len(blocks) == 1
    assert isinstance(blocks[0], TextContent)
    return json.loads(blocks[0].text)


def wire_size(result):
    return len(result.model_dump_json(by_alias=True, exclude_none=True).encode("utf-8"))


def viewer_handler(server_factory):
    handler = server_factory.call_args_list[0].args[1]
    assert issubclass(handler, workspace_module.AuditViewerHandler)
    assert len(handler.viewer_instance_id) == 32
    return handler


async def test_workspace_result_is_single_compact_payload_and_output_is_pageable(local):
    executor, root = local
    workspace = Workspace("efficiency", "remote", str(root), executor)
    server = create_server(workspace)

    # Ordinary output fits the native-like default without an extra fetch;
    # the complete stream remains pageable and explicit small caps still work.
    blocks = await server.call_tool(
        "exec_command",
        {"command": "python3 -c 'print(\"x\" * 5000, end=\"\")'"},
    )
    command_result = decode_content(blocks)
    assert command_result["exit_code"] == 0
    assert command_result["truncated"] is False
    assert command_result["output_id"]
    assert command_result["stdout"] == "x" * 5000
    capped = decode_content(await server.call_tool(
        "exec_command", {"command": "python3 -c 'print(\"x\" * 5000, end=\"\")'", "max_bytes": 2048}
    ))
    assert capped['truncated'] is True and '[truncated bytes]' in capped['stdout']

    # FastMCP's previous default emitted pretty JSON text and the same object
    # again as structuredContent. Compare that exact envelope with this one.
    after = CallToolResult(content=list(blocks))
    legacy = CallToolResult(
        content=[TextContent(type="text", text=json.dumps(command_result, ensure_ascii=False, indent=2))],
        structuredContent=command_result,
    )
    after_bytes, legacy_bytes = wire_size(after), wire_size(legacy)
    assert after_bytes < legacy_bytes * 0.60

    pages = []
    cursor = 0
    while True:
        page = decode_content(await server.call_tool(
            "read_output",
            {"output_id": command_result["output_id"], "stream": "stdout", "cursor": cursor, "max_bytes": 4096},
        ))
        pages.append(page["content"])
        cursor = page["next_cursor"]
        if not page["truncated"]:
            break
    assert "".join(pages) == "x" * 5000

    # A nonzero command retains its diagnostic fields in the compact response.
    failure = decode_content(await server.call_tool(
        "exec_command", {"command": "printf diagnostic >&2; exit 23"}
    ))
    assert failure["exit_code"] == 23
    assert failure["stderr"].strip() == "diagnostic"
    assert failure["execution_state"] == "finished"
    assert failure["output_id"]

    # Edit diffs remain immediately visible and can also be read from the
    # saved diff stream if their preview is capped.
    written = decode_content(await server.call_tool(
        "write_file", {"path": "review.txt", "content": "before\n"}
    ))
    assert written["success"]
    diff = decode_content(await server.call_tool(
        "patch_file", {"path": "review.txt", "old": "before", "new": "after"}
    ))
    assert "-before" in diff["diff"] and "+after" in diff["diff"]
    assert diff["output_id"]
    saved_diff = decode_content(await server.call_tool(
        "read_output", {"output_id": diff["output_id"], "stream": "diff", "max_bytes": 4096}
    ))
    assert "-before" in saved_diff["content"] and "+after" in saved_diff["content"]


def test_local_audit_viewer_is_optional_loopback_and_closes_on_exit():
    events = []
    viewer = MagicMock()
    viewer.server_port = 8767
    thread = MagicMock()
    server_factory = MagicMock(return_value=viewer)
    thread_factory = MagicMock(return_value=thread)
    viewer.shutdown.side_effect = lambda: events.append("shutdown")
    viewer.server_close.side_effect = lambda: events.append("close")
    thread.join.side_effect = lambda: events.append("join")

    with patch.object(workspace_module, "ThreadingHTTPServer", server_factory), \
         patch.object(workspace_module, "Thread", thread_factory):
        with workspace_module._local_audit_viewer(None) as disabled:
            assert disabled is None
        server_factory.assert_not_called()

        with pytest.raises(RuntimeError, match="leave MCP"):
            with workspace_module._local_audit_viewer(8767) as active:
                assert active is viewer
                handler = viewer_handler(server_factory)
                server_factory.assert_called_once_with(("127.0.0.1", 8767), handler)
                assert active.viewer_url == f"http://127.0.0.1:8767/v/{handler.viewer_instance_id}"
                thread_factory.assert_called_once_with(
                    target=viewer.serve_forever,
                    kwargs={"poll_interval": 0.1},
                    name="remote-workbench-audit-viewer-8767",
                    daemon=True,
                )
                thread.start.assert_called_once_with()
                raise RuntimeError("leave MCP")

    assert events == ["shutdown", "close", "join"]


def test_local_audit_viewer_falls_back_only_when_requested_port_is_occupied():
    viewer = MagicMock()
    viewer.server_port = 43210
    bind_error = OSError(errno.EADDRINUSE, "Address already in use")
    server_factory = MagicMock(side_effect=[bind_error, viewer])
    thread = MagicMock()

    with patch.object(workspace_module, "ThreadingHTTPServer", server_factory), \
         patch.object(workspace_module, "Thread", return_value=thread) as thread_factory:
        with workspace_module._local_audit_viewer(8768) as active:
            assert active is viewer

    handler = viewer_handler(server_factory)
    assert [call.args for call in server_factory.call_args_list] == [
        (("127.0.0.1", 8768), handler),
        (("127.0.0.1", 0), handler),
    ]
    thread_factory.assert_called_once_with(
        target=viewer.serve_forever,
        kwargs={"poll_interval": 0.1},
        name="remote-workbench-audit-viewer-43210",
        daemon=True,
    )
    thread.start.assert_called_once_with()
    viewer.shutdown.assert_called_once_with()
    viewer.server_close.assert_called_once_with()
    thread.join.assert_called_once_with()


@pytest.mark.parametrize("error_number", [errno.EACCES, errno.EADDRNOTAVAIL])
def test_local_audit_viewer_preserves_unrelated_bind_errors(error_number):
    bind_error = OSError(error_number, "bind failed")
    server_factory = MagicMock(side_effect=bind_error)
    thread_factory = MagicMock()

    with patch.object(workspace_module, "ThreadingHTTPServer", server_factory), \
         patch.object(workspace_module, "Thread", thread_factory):
        with pytest.raises(OSError) as caught:
            with workspace_module._local_audit_viewer(8768):
                pytest.fail("viewer must not start when bind fails")

    assert caught.value is bind_error
    server_factory.assert_called_once_with(("127.0.0.1", 8768), viewer_handler(server_factory))
    thread_factory.assert_not_called()


def test_local_audit_viewer_preserves_fallback_bind_error():
    occupied_error = OSError(errno.EADDRINUSE, "Address already in use")
    fallback_error = OSError(errno.EMFILE, "Too many open files")
    server_factory = MagicMock(side_effect=[occupied_error, fallback_error])
    thread_factory = MagicMock()

    with patch.object(workspace_module, "ThreadingHTTPServer", server_factory), \
         patch.object(workspace_module, "Thread", thread_factory):
        with pytest.raises(OSError) as caught:
            with workspace_module._local_audit_viewer(8768):
                pytest.fail("viewer must not start when the fallback bind fails")

    assert caught.value is fallback_error
    handler = viewer_handler(server_factory)
    assert [call.args for call in server_factory.call_args_list] == [
        (("127.0.0.1", 8768), handler),
        (("127.0.0.1", 0), handler),
    ]
    thread_factory.assert_not_called()


def test_local_audit_viewer_uses_separate_live_listener_when_port_is_occupied():
    def assert_serving(viewer):
        with urlopen(viewer.viewer_url + "/", timeout=2) as response:
            assert response.status == 200
            assert b"Remote Workbench" in response.read(4096)
            assert response.headers["X-Remote-Workbench-Viewer"] == viewer.RequestHandlerClass.viewer_instance_id

    with workspace_module._local_audit_viewer(0) as first:
        occupied_port = first.server_port
        assert occupied_port > 0
        assert_serving(first)

        with workspace_module._local_audit_viewer(occupied_port) as second:
            assert second.server_port != occupied_port
            assert first.RequestHandlerClass.viewer_instance_id != second.RequestHandlerClass.viewer_instance_id
            assert_serving(first)
            assert_serving(second)

        assert second.fileno() == -1
        assert first.fileno() >= 0
        assert_serving(first)

    assert first.fileno() == -1


def test_viewer_does_not_reuse_or_interrupt_another_projects_html():
    class OtherProject(BaseHTTPRequestHandler):
        def do_GET(self):
            body = b"another project's HTML"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    other = ThreadingHTTPServer(("127.0.0.1", 0), OtherProject)
    thread = Thread(target=other.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True)
    thread.start()
    try:
        other_url = f"http://127.0.0.1:{other.server_port}/"
        with workspace_module._local_audit_viewer(other.server_port) as viewer:
            assert viewer.server_port != other.server_port
            with urlopen(other_url, timeout=2) as response:
                assert response.read() == b"another project's HTML"
            with urlopen(viewer.viewer_url + "/", timeout=2) as response:
                assert b"Remote Workbench" in response.read()
        with urlopen(other_url, timeout=2) as response:
            assert response.read() == b"another project's HTML"
    finally:
        other.shutdown()
        other.server_close()
        thread.join()


def test_old_viewer_url_is_rejected_when_a_new_project_reuses_its_port(tmp_path):
    with workspace_module._local_audit_viewer(0, audit_dir=tmp_path / "old") as previous:
        port = previous.server_port
        old_url = previous.viewer_url
    with workspace_module._local_audit_viewer(port, audit_dir=tmp_path / "new") as current:
        assert current.server_port == port
        assert current.viewer_url != old_url
        for suffix in ("/", "/api/records"):
            with pytest.raises(HTTPError) as caught:
                urlopen(old_url + suffix, timeout=2)
            assert caught.value.code == 404
        with urlopen(current.viewer_url + "/api/records", timeout=2) as response:
            assert json.load(response) == []


async def test_workspace_info_includes_only_the_viewer_url_for_its_mcp(local):
    executor, root = local
    executor.check_connection = AsyncMock(return_value=(False, "Disconnected"))
    workspace = Workspace("viewer-link", "remote", str(root), executor)
    viewer_url = "http://127.0.0.1:43210"

    with_viewer = create_server(workspace, viewer_url=viewer_url)
    response = decode_content(await with_viewer.call_tool("workspace_info", {}))
    assert response["viewer_url"] == viewer_url

    without_viewer = create_server(workspace)
    response = decode_content(await without_viewer.call_tool("workspace_info", {}))
    assert "viewer_url" not in response


@pytest.mark.parametrize("port", [-1, 65536, True])
def test_local_audit_viewer_rejects_invalid_port(port):
    with patch.object(workspace_module, "ThreadingHTTPServer") as server_factory:
        with pytest.raises(ValueError, match="viewer port"):
            with workspace_module._local_audit_viewer(port):
                pass
        server_factory.assert_not_called()
