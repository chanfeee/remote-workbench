"""Explicit composition saves client round trips, never infers verification intent."""
import json
import subprocess
import sys
import pytest
from mcp.server.fastmcp.exceptions import ToolError
from tests.test_session_server import call, project, router_at
from remote_workbench.session_server import create_server


async def prepared(project):
    router, server, source, other = project
    subprocess.run(['git','init','-q',str(source)],check=True)
    (source/'notes.txt').write_text('read only\n')
    result = await call(server,'a','workspace_switch',host='a',cwd=str(source),
        command=['pwd'],read_files=['notes.txt'],edit_files=['value.txt'])
    assert result['initial']['exit_code']==0
    assert len(result['files'])==2 and 'edit_id' not in result['files'][0]
    assert result['files'][0]['content']=='1\tread only'
    assert 'local_path' not in result['files'][1]
    return router,server,source,other,result['files'][1]['edit_id']


async def test_bind_read_edit_and_checks_need_two_tool_calls(project):
    router,server,source,_,ref = await prepared(project)
    result=await call(server,'a','apply_patch',edits=[{'edit_id':ref,'new':['changed']}],
        command=[sys.executable,'-c','from pathlib import Path;assert Path("value.txt").read_text()=="changed\\n";print("checked")'])
    assert result['success'] and result['command_result']['exit_code']==0
    assert result['command_result']['stdout'].strip()=='checked'
    record=await call(server,'a','audit_get',operation_id=result['output_id'])
    assert record['following_output_id']==result['command_result']['output_id']
    assert record['following_exit_code']==0 and record['following_execution_state']=='finished'
    following=await call(server,'a','audit_get',operation_id=result['command_result']['output_id'])
    assert following['parent_id']==result['command_result']['operation_id']
    middle=await call(server,'a','audit_get',operation_id=following['parent_id'])
    assert middle['parent_id']==result['output_id']
    assert router.resources.active()==[]
    # A known-content cleanup can use the returned reference without rereading.
    next_ref=result['edits'][0]['edit_id']
    changed=await call(server,'a','apply_patch',edits=[{'edit_id':next_ref,'new':['next']}])
    assert changed['success'] and (source/'value.txt').read_text()=='next\n'


async def test_conflict_never_runs_following_command(project):
    _,server,source,_,ref=await prepared(project)
    (source/'value.txt').write_text('external\n')
    result=await call(server,'a','apply_patch',edits=[{'edit_id':ref,'new':['changed']}],
        command=[sys.executable,'-c','from pathlib import Path;Path("should-not-run").touch()'])
    assert result['error']=='conflict' and result['command_skipped']=='apply_not_successful'
    assert not (source/'should-not-run').exists()


@pytest.mark.parametrize('kind',['preview','auth','compute'])
async def test_invalid_followup_is_refused_before_any_edit(project,monkeypatch,kind):
    router,server,source,_,ref=await prepared(project)
    ex=router.snapshot(router.registry.get_or_create('a')).workspace.executor
    async def forbidden(*args,**kwargs):pytest.fail('must not submit either phase')
    monkeypatch.setattr(ex,'run',forbidden)
    options={'command':['pwd']}
    if kind=='preview':options['check_only']=True
    elif kind=='auth':options['command']=['sshpass','-p','private-marker','ssh','host']
    else:options['command']=['make','-j64']
    if kind=='preview':
        with pytest.raises(ToolError,match='check_only'):
            await call(server,'a','apply_patch',edits=[{'edit_id':ref,'new':['changed']}],**options)
    else:
        value=await call(server,'a','apply_patch',edits=[{'edit_id':ref,'new':['changed']}],**options)
        assert value['executed'] is False and 'private-marker' not in json.dumps(value)
    assert (source/'value.txt').read_text()=='original\n' and router.resources.active()==[]


async def test_failed_check_does_not_hide_successful_write_or_hold_terminal_claim(project):
    router,server,source,_,ref=await prepared(project)
    value=await call(server,'a','apply_patch',edits=[{'edit_id':ref,'new':['changed']}],
        command=[sys.executable,'-c','raise SystemExit(7)'])
    assert value['success'] and value['command_result']['exit_code']==7
    assert (source/'value.txt').read_text()=='changed\n' and router.resources.active()==[]


async def test_command_side_effect_makes_returned_apply_snapshot_stale(project):
    _,server,source,_,ref=await prepared(project)
    value=await call(server,'a','apply_patch',edits=[{'edit_id':ref,'new':['changed']}],
        command=[sys.executable,'-c','from pathlib import Path;Path("value.txt").write_text("formatted\\n")'])
    next_ref=value['edits'][0]['edit_id']
    stale=await call(server,'a','apply_patch',edits=[{'edit_id':next_ref,'new':['next']}])
    assert stale['error']=='conflict' and (source/'value.txt').read_text()=='formatted\n'


async def test_read_failure_is_per_file_and_binding_survives(project):
    _,server,source,_=project
    value=await call(server,'a','workspace_switch',host='a',cwd=str(source),
        read_files=['missing.txt','value.txt'])
    assert value['bound'] and value['files'][0]['error']
    assert value['files'][1]['content']=='1\toriginal'


async def test_failed_initial_command_does_not_continue_reads(project):
    _,server,source,_=project
    value=await call(server,'a','workspace_switch',host='a',cwd=str(source),
        command=[sys.executable,'-c','raise SystemExit(4)'],edit_files=['value.txt'])
    assert value['bound'] and value['initial']['exit_code']==4
    assert value['files_skipped']=='initial_command_not_successful' and 'files' not in value


@pytest.mark.parametrize('files',[['same','same'],['x']*17,['bad\x00path'],['~/guess']])
async def test_invalid_read_selection_rejected_before_remote(tmp_path,files):
    router=router_at(tmp_path)
    with pytest.raises(ToolError):
        await call(create_server(router),'a','workspace_switch',host='a',cwd='/unused',read_files=files)
    assert not router._executors


async def test_followup_keeps_original_binding_and_write_claim(project,monkeypatch):
    import asyncio
    router,server,source,other,ref=await prepared(project)
    ex=router.snapshot(router.registry.get_or_create('a')).workspace.executor
    original=ex.run;entered=asyncio.Event();release=asyncio.Event()
    async def pause(*args,**kwargs):
        if kwargs.get('display_command')=='pwd':
            assert len(router.resources.active())==1
            entered.set();await release.wait()
        return await original(*args,**kwargs)
    monkeypatch.setattr(ex,'run',pause)
    running=asyncio.create_task(call(server,'a','apply_patch',edits=[{'edit_id':ref,'new':['changed']}],command=['pwd']))
    await asyncio.wait_for(entered.wait(),5)
    switching=asyncio.create_task(call(server,'a','workspace_switch',host='b',cwd=str(other)))
    await asyncio.sleep(0)
    assert not switching.done()
    release.set()
    value=await asyncio.wait_for(running,5)
    assert value['command_result']['stdout'].strip()==str(source)
    assert (await switching)['host']=='b' and router.resources.active()==[]


async def test_unknown_followup_does_not_claim_write_was_rolled_back(project,monkeypatch):
    router,server,source,_,ref=await prepared(project)
    ex=router.snapshot(router.registry.get_or_create('a')).workspace.executor
    original=ex.run
    async def fail(*args,**kwargs):
        if kwargs.get('display_command')=='pwd':raise OSError('simulated reply loss')
        return await original(*args,**kwargs)
    monkeypatch.setattr(ex,'run',fail)
    value=await call(server,'a','apply_patch',edits=[{'edit_id':ref,'new':['changed']}],command=['pwd'])
    assert value['success'] and value['command_result']['execution_state']=='unknown'
    assert (source/'value.txt').read_text()=='changed\n'
    assert len(router.resources.active())==1
