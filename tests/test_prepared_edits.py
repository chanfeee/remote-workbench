"""Snapshot-backed edits: exact bytes, original positions and stale-read protection."""
import json
from pathlib import Path
import shutil
import stat
import sys
import subprocess

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from remote_workbench import audit
from remote_workbench.session_server import create_server
from tests.test_session_server import call, project, router_at


@pytest.fixture
def prepared_files():
    paths = []
    async def read(server, path="value.txt", **kwargs):
        result = await call(server, "a", "workspace_tools", tool="read_file",
                            arguments=dict(path=path, for_edit=True, **kwargs))
        if result.get("local_path"):
            paths.append(Path(result["local_path"]).parent)
        return result
    yield read
    for path in paths:
        shutil.rmtree(path, ignore_errors=True)


async def bind(project):
    router, server, source, other = project
    await call(server, "a", "workspace_switch", host="a", cwd=str(source))
    await call(server, "a", "exec_command", command=["git", "init", "-q"])
    return router, server, source, other


async def test_range_uses_read_snapshot_instead_of_copied_old_context(project, prepared_files):
    _, server, source, _ = await bind(project)
    original = "import csv\n\n\ndef summarize():\n    return 1\n"
    (source/"value.txt").write_text(original)
    ref = await prepared_files(server)
    assert ref["total_lines"] == 5
    # The observed benchmark invented an extra blank line in old[]. No old[] is needed here.
    changed = await call(server, "a", "apply_patch", edits=[dict(
        edit_id=ref["edit_id"], start_line=4, end_line=5, new=["def summarize():", "    return 2"])])
    assert changed["success"]
    assert (source/"value.txt").read_text() == original.replace("return 1", "return 2")


@pytest.mark.parametrize("ending", ["\n", "\r\n", ""])
async def test_staged_source_bytes_survive_bridge_and_remote_apply(project, prepared_files, ending):
    router, server, source, _ = await bind(project)
    (source/"value.txt").chmod(0o750)
    ref = await prepared_files(server)
    # Server-generated repr has exactly one source escape, independent of JS/JSON nesting.
    text = "fixture = " + repr("value\n1\n3\n") + "\n" + "literal = " + repr("\\n `ticks` ${literal} 中文")
    text = text.replace("\n", "\r\n") if ending == "\r\n" else text
    text += ending
    data = text.encode()
    Path(ref["local_path"]).write_bytes(data)
    from remote_workbench.call_source import render_call
    from tests.test_tool_call_syntax import run_source
    arguments = {"edits": [{"edit_id": ref["edit_id"]}]}
    generated = run_source(render_call("apply_patch", arguments))
    assert len(render_call("apply_patch", arguments)) < 600
    assert generated["calls"] == [arguments]
    result = await call(server, "a", "apply_patch", **generated["calls"][0])
    assert result["success"] and (source/"value.txt").read_bytes() == data
    assert stat.S_IMODE((source/"value.txt").stat().st_mode) == 0o750
    verified = await call(server, "a", "exec_command", command=[
        sys.executable, "-c", "exec(open('value.txt').read()); assert fixture.splitlines() == ['value','1','3']"])
    assert verified["exit_code"] == 0
    assert router.resources.active() == []


async def test_external_change_rejects_whole_batch_and_preview_is_read_only(project, prepared_files):
    _, server, source, _ = await bind(project)
    (source/"second.txt").write_text("second\n")
    first = await prepared_files(server)
    second = await prepared_files(server, "second.txt")
    Path(first["local_path"]).write_text("changed\n")
    Path(second["local_path"]).write_text("changed second\n")
    edits = [{"edit_id": first["edit_id"]}, {"edit_id": second["edit_id"]}]
    preview = await call(server, "a", "apply_patch", edits=edits, check_only=True)
    assert preview["success"] and (source/"value.txt").read_text() == "original\n"
    (source/"second.txt").write_text("external\n")
    conflict = await call(server, "a", "apply_patch", edits=edits)
    assert conflict["error"] == "conflict" and (source/"value.txt").read_text() == "original\n"
    assert (source/"second.txt").read_text() == "external\n"


async def test_unrelated_line_change_is_still_a_conflict(project, prepared_files):
    _, server, source, _ = await bind(project)
    (source/"value.txt").write_text("one\ntwo\nthree\n")
    ref = await prepared_files(server)
    (source/"value.txt").write_text("one\ntwo\nexternal\n")
    result = await call(server, "a", "apply_patch", edits=[dict(
        edit_id=ref["edit_id"], start_line=2, end_line=2, new=["changed"])])
    assert result["error"] == "conflict"
    assert (source/"value.txt").read_text() == "one\ntwo\nexternal\n"


async def test_snapshot_origin_is_pinned_and_survives_mcp_restart(project, prepared_files, monkeypatch):
    router, server, source, other = await bind(project)
    ref = await prepared_files(server)
    Path(ref["local_path"]).write_text("after restart\n")
    await call(server, "a", "workspace_switch", host="b", cwd=str(other))
    current = router.snapshot(router.registry.get_or_create("a")).workspace.executor
    async def forbidden(*args, **kwargs):
        pytest.fail("invalid local reference must not reach SSH")
    with monkeypatch.context() as m:
        m.setattr(current, "run", forbidden)
        with pytest.raises(ToolError, match="another target"):
            await call(server, "a", "apply_patch", edits=[{"edit_id":ref["edit_id"]}])
    assert router.resources.active() == []
    await call(server, "other-chat", "workspace_switch", host="a", cwd=str(source))
    with pytest.raises(ToolError, match="Unknown edit_id"):
        await call(server, "other-chat", "apply_patch", edits=[{"edit_id":ref["edit_id"]}])
    await call(server, "a", "workspace_switch", host="a", cwd=str(source))
    restarted = create_server(router_at(router.state_dir))
    assert (await call(restarted, "a", "apply_patch", edits=[{"edit_id":ref["edit_id"]}]))["success"]
    assert (source/"value.txt").read_text() == "after restart\n"


async def test_snapshot_contains_full_file_while_preview_stays_bounded(project, prepared_files):
    router, server, source, _ = await bind(project)
    data = b"line contents\n" * 1000
    (source/"value.txt").write_bytes(data)
    ref = await prepared_files(server, limit=2, max_bytes=256)
    assert ref["truncated"] and len(ref["content"].encode()) <= 256
    assert Path(ref["local_path"]).read_bytes() == data
    assert not any(k.startswith("_edit_") for k in ref)
    noop = await call(server, "a", "apply_patch", edits=[{"edit_id":ref["edit_id"]}])
    assert noop["success"] and noop["changed"] is False
    info = await call(server, "a", "workspace_info")
    assert info["runtime"]["data_dir"] == str(router.state_dir)
    assert info["runtime"]["package_path"].endswith("/remote_workbench")


@pytest.mark.parametrize("damage", ["symlink", "original", "range", "overlap"])
async def test_invalid_snapshot_fails_before_ssh_and_write_claim(project, prepared_files, monkeypatch, damage):
    router, server, source, _ = await bind(project)
    ref = await prepared_files(server)
    edits = [{"edit_id":ref["edit_id"]}]
    if damage == "symlink":
        working = Path(ref["local_path"]); working.unlink(); working.symlink_to(source/"value.txt")
    elif damage == "original":
        state = router.registry.get_or_create("a")
        with audit.scoped_root(router.audit_dir(state.workspace_id)):
            (audit.directory(ref["edit_id"])/"edit/original").write_text("tampered")
    elif damage == "range":
        edits = [dict(edit_id=ref["edit_id"], start_line=1, end_line=99, new=["new"])]
    else:
        item = dict(edit_id=ref["edit_id"], start_line=1, end_line=1, new=["new"])
        edits = [item, item]
    ex = router.snapshot(router.registry.get_or_create("a")).workspace.executor
    async def forbidden(*args, **kwargs):
        pytest.fail("invalid snapshot must not contact SSH")
    monkeypatch.setattr(ex, "run", forbidden)
    with pytest.raises(ToolError):
        await call(server, "a", "apply_patch", edits=edits)
    assert router.resources.active() == []
    assert (source/"value.txt").read_text() == "original\n"


async def test_regular_read_has_no_staging_or_git_requirement(project):
    _, server, source, _ = project
    await call(server, "a", "workspace_switch", host="a", cwd=str(source))
    ordinary = await call(server, "a", "workspace_tools", tool="read_file", arguments={"path":"value.txt"})
    assert "edit_id" not in ordinary and ordinary["content"] == "1\toriginal"
    unavailable = await call(server, "a", "workspace_tools", tool="read_file", arguments={"path":"value.txt","for_edit":True})
    assert unavailable["error"] == "not_repository" and "edit_id" not in unavailable


@pytest.mark.parametrize('before,expected', [
    (b'original\n', b'literal \\n `ticks` ${value} \xe4\xb8\xad\xe6\x96\x87\nsecond\n'),
    (b'original\r\n', b'literal \\n `ticks` ${value} \xe4\xb8\xad\xe6\x96\x87\r\nsecond\r\n'),
    (b'original', b'literal \\n `ticks` ${value} \xe4\xb8\xad\xe6\x96\x87\nsecond'),
    (b'', b'literal \\n `ticks` ${value} \xe4\xb8\xad\xe6\x96\x87\nsecond\n'),
])
async def test_primary_read_and_whole_file_data_need_no_native_patch(project, before, expected):
    _, server, source, _ = await bind(project)
    (source/'value.txt').write_bytes(before)
    ref = await call(server, 'a', 'read_file', path='value.txt', for_edit=True)
    assert 'edit_id' in ref and 'local_path' not in ref
    assert ref['path'] == str(source/'value.txt')
    with pytest.raises(ToolError, match='no local copy'):
        await call(server, 'a', 'apply_patch', edits=[{'edit_id':ref['edit_id']}])
    value = await call(server, 'a', 'apply_patch', edits=[{
        'edit_id': ref['edit_id'], 'new': [r'literal \n `ticks` ${value} 中文', 'second']}])
    assert value['success'] and (source/'value.txt').read_bytes() == expected


async def test_primary_whole_file_rejects_stale_snapshot_and_mixed_ranges(project):
    _, server, source, _ = await bind(project)
    ref = await call(server, 'a', 'read_file', path='value.txt', for_edit=True)
    edit = {'edit_id':ref['edit_id'], 'new':['changed']}
    with pytest.raises(ToolError, match='cannot be mixed'):
        await call(server, 'a', 'apply_patch', edits=[edit, dict(edit, start_line=1, end_line=1)])
    (source/'value.txt').write_text('external\n')
    value = await call(server, 'a', 'apply_patch', edits=[edit])
    assert value['error'] == 'conflict' and (source/'value.txt').read_text() == 'external\n'


def test_partial_write_failure_reports_applied_and_pending_paths(tmp_path, monkeypatch):
    from remote_workbench import remote_worker as worker
    from remote_workbench.native_patch import prepared_file_patch
    import hashlib
    repo = tmp_path/'repo'; repo.mkdir()
    subprocess.run(['git','init','-q',str(repo)],check=True)
    monkeypatch.chdir(repo)
    monkeypatch.setenv('REMOTE_WORKBENCH_REMOTE_STATE_DIR',str(tmp_path/'state'))
    original = {str(repo/name):b'before\n' for name in ['one.txt','two.txt']}
    for path,data in original.items():Path(path).write_bytes(data)
    contents = {path:'after\n' for path in original}
    real = worker.atomic_write
    def fail_second(path,data,previous):
        if path.name=='two.txt':raise ValueError('external modification')
        return real(path,data,previous)
    monkeypatch.setattr(worker,'atomic_write',fail_second)
    result = worker.apply_patch({'patch':prepared_file_patch(original,contents,repo),
        'expected_files':{p:hashlib.sha256(b).hexdigest() for p,b in original.items()},
        'expected_repository':str(repo),'prepared_contents':contents})
    assert result['error']=='partial_apply'
    assert result['applied_paths']==[str(repo/'one.txt')]
    assert result['pending_paths']==[str(repo/'two.txt')]
    assert (repo/'one.txt').read_text()=='after\n' and (repo/'two.txt').read_text()=='before\n'


async def test_known_prepared_paths_extend_write_coordination(project, prepared_files):
    router, server, source, _ = await bind(project)
    a,b=source/'a',source/'b';a.mkdir();b.mkdir();(b/'item.py').write_text('value=1\n')
    await call(server,'a','workspace_switch',host='a',cwd=str(a))
    other=await call(server,'b','workspace_switch',host='a',cwd=str(b))
    ref=await prepared_files(server,str(b/'item.py'))
    Path(ref['local_path']).write_text('value=2\n')
    ex=router.snapshot(router.registry.get_or_create('a')).workspace.executor
    held=router.resources.acquire(ex.config.ssh_target,str(b),other['workspace_id'],'exec_command')
    with pytest.raises(ToolError,match='Managed directory busy'):
        await call(server,'a','apply_patch',edits=[{'edit_id':ref['edit_id']}])
    assert (b/'item.py').read_text()=='value=1\n'
    assert [c['claim_id'] for c in router.resources.active()]==[held]
    router.resources.finish(held,{'executed':False})
