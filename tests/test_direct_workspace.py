"""Direct routing: local simulated SSH, never a real cluster acceptance."""
import asyncio
import json
from pathlib import Path

import pytest
from mcp.server.fastmcp.exceptions import ToolError
from remote_workbench.resource_claims import ResourceClaims
from remote_workbench.session_server import create_server
from tests.test_session_server import call, project, router_at


async def test_default_direct_edits_source_without_copy(project):
    router, server, source, _ = project
    direct = await call(server, "a", "workspace_switch", host="a", cwd=str(source))
    assert direct["cwd"] == direct["source_cwd"] == str(source.resolve())
    assert direct["directory_mode"] == "direct" and not direct["project_isolated"]
    read = await call(server, "a", "read_file", path="value.txt")
    write = await call(server, "a", "write_file", path="value.txt", content="direct edit\n", expected_sha256=read["sha256"])
    assert write["success"] and (source / "value.txt").read_text() == "direct edit\n"
    snapshot = router.snapshot(router.registry.get(direct["workspace_id"]))
    runtime = snapshot.workspace.executor.remote_state_dir
    assert not Path(runtime).is_relative_to(source)
    info = await call(server, "a", "exec_command", command="pwd")
    assert info["exit_code"] == 0 and info["stdout"].strip() == str(source.resolve())
    restarted = create_server(router_at(router.state_dir))
    saved = await call(restarted, "a", "workspace_info")
    assert saved["directory_mode"] == "direct" and saved["cwd"] == str(source.resolve())
    assert not (source.parent / ".remote-workbench-sessions").exists()



async def test_shared_direct_blocks_writer_but_allows_reads_and_sibling(project):
    router, server, source, other = project
    for thread in ("a", "b"):
        bound = await call(server, thread, "workspace_switch", host="a", cwd=str(source))
        assert bound["cwd"] == str(source.resolve())
    a = router.registry.get_or_create("a")
    claim = router.resources.acquire("host-a", str(source), a.workspace_id, "start_job")
    with pytest.raises(ToolError, match="busy or completion unknown"):
        await call(server, "b", "write_file", path="value.txt", content="blocked")
    assert "original" in (await call(server, "b", "read_file", path="value.txt"))["content"]
    await call(server, "b", "workspace_switch", host="a", cwd=str(other))
    assert (await call(server, "b", "write_file", path="value.txt", content="sibling"))["success"]
    router.resources.finish(claim, {"executed":False})


def test_overlaps_persistent_unknown_and_collector_completion(tmp_path):
    resources = ResourceClaims(tmp_path)
    claim = resources.acquire("user@host", "/repo/a", "w", "start_job")
    resources.attach_job(claim, "j")
    resources.finish(claim, {"job_id":"j", "execution_state":"unknown"}, background=True)
    resources = ResourceClaims(tmp_path)
    for cwd in ("/repo", "/repo/a", "/repo/a/sub"):
        with pytest.raises(ValueError, match="busy"):
            resources.acquire("user@host", cwd, "other", "exec_command")
    sibling = resources.acquire("user@host", "/repo/b", "other", "write_file")
    resources.finish(sibling, {"success":True})
    record = tmp_path / "audit/w/output/record.json"
    record.parent.mkdir(parents=True)
    record.write_text(json.dumps({"execution_state":"unknown", "sync_state":"complete"}))
    resources.reconcile("w", "j", {"job_output_id":"output"})
    assert len(resources.active()) == 1
    record.write_text(json.dumps({"execution_state":"finished", "sync_state":"complete"}))
    assert resources.active() == []


async def test_new_default_never_prepares_copy_and_subdirectory_stays_selected(project, monkeypatch):
    router, server, source, _ = project
    child = source / "sub"
    child.mkdir()
    bound = await call(server, "fresh", "workspace_switch", host="a", cwd=str(child))
    assert bound["cwd"] == str(child.resolve())
    assert not (source.parent / ".remote-workbench-sessions").exists()
    result = await call(server, "fresh", "exec_command", command="pwd")
    assert result["stdout"].strip() == str(child.resolve())


def test_shell_unknown_claim_cannot_be_released_as_plain_edit(tmp_path):
    resources = ResourceClaims(tmp_path)
    claim = resources.acquire("host", "/repo", "w", "exec_command")
    resources.finish(claim, {"error":"transport_lost"}, shell=True)
    assert len(resources.active()) == 1
    with pytest.raises(ValueError):
        ResourceClaims(tmp_path).acquire("host", "/repo/a", "other", "write_file")
