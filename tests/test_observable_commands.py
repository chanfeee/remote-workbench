"""Normal command/patch operations retain user records without model bookkeeping."""
import json
import sys
from http.server import ThreadingHTTPServer
from threading import Thread

import httpx
import pytest
from mcp.server.fastmcp.exceptions import ToolError

from remote_workbench import audit
from remote_workbench.audit_web import handler_for_directory
from tests.test_session_server import call, project


async def test_first_command_binds_executes_and_records_stdin_without_setup_call(project):
    router,server,source,_=project
    data='"quotes" \\ literal\\n 中文\n'
    result=await call(server,'a','exec_command',host='a',cwd=str(source),
        command=[sys.executable,'-c','import sys;sys.stdout.write(sys.stdin.read())'],stdin=data)
    assert result['exit_code']==0 and result['stdout']==data
    assert result['host']=='a' and result['cwd']==str(source) and result['operation_log']
    state=router.registry.get_or_create('a')
    assert state.host=='a' and state.cwd==str(source)
    with audit.scoped_root(router.audit_dir(state.workspace_id)):
        record=json.loads((audit.directory(result['output_id'])/'record.json').read_text())
        assert (audit.directory(result['output_id'])/'stdin').read_text()==data
        assert (audit.directory(result['output_id'])/'stdout').read_text()==data
        assert record['host']=='host-a' and record['actual_cwd']==str(source)
        assert record['target_id']==state.target_id and 'sys.stdin.read' in record['command']
    # No second binding is required, and another chat stays local.
    later=await call(server,'a','exec_command',command=['pwd'])
    assert later['stdout'].strip()==str(source)
    with pytest.raises(ToolError,match='no remote workspace'):
        await call(server,'b','exec_command',command=['pwd'])


async def test_native_patch_creates_file_and_returns_checks_and_user_diff_without_snapshot(project):
    router,server,source,_=project
    await call(server,'a','exec_command',host='a',cwd=str(source),command=['pwd'])
    patch='*** Begin Patch\n*** Update File: value.txt\n@@\n-original\n+changed\n*** Add File: run.py\n+print("ready")\n*** End Patch\n'
    result=await call(server,'a','apply_patch',patch=patch,command=[sys.executable,'run.py'])
    assert result['success'] and result['command_result']['stdout'].strip()=='ready'
    assert (source/'value.txt').read_text()=='changed\n' and (source/'run.py').read_text()=='print("ready")\n'
    assert not (source/'.git').exists() and not list(source.rglob('.remote-workbench-sessions'))
    state=router.registry.get_or_create('a')
    with audit.scoped_root(router.audit_dir(state.workspace_id)):
        record=json.loads((audit.directory(result['output_id'])/'record.json').read_text())
        diff=(audit.directory(result['output_id'])/'diff').read_text()
        assert record['patch_result']=='applied' and record['following_exit_code']==0
        assert 'value.txt' in diff and 'run.py' in diff and '-original' in diff and '+changed' in diff
        assert '"ready"' in diff
    viewer=ThreadingHTTPServer(('127.0.0.1',0),handler_for_directory(router.audit_dir(state.workspace_id)))
    thread=Thread(target=viewer.serve_forever,daemon=True);thread.start()
    try:
        with httpx.Client(base_url=f'http://127.0.0.1:{viewer.server_port}',trust_env=False) as client:
            shown=client.get('/api/output',params={'id':result['output_id'],'stream':'diff'}).json()
            record=client.get('/api/record',params={'id':result['output_id']}).json()
        assert 'run.py' in shown['content'] and '+changed' in shown['content']
        assert record['patch_result']=='applied'
    finally:
        viewer.shutdown();viewer.server_close();thread.join()
    assert router.resources.active()==[]


async def test_patch_check_returns_original_test_report_in_same_reply(project):
    router,server,source,_=project
    await call(server,'a','exec_command',host='a',cwd=str(source),command=['pwd'])
    patch=('*** Begin Patch\n*** Add File: test_ready.py\n'
           '+import unittest\n+class Ready(unittest.TestCase):\n'
           '+    def test_ready(self):\n+        self.assertEqual(2 + 2, 4)\n'
           '*** End Patch\n')
    result=await call(server,'a','apply_patch',patch=patch,
        command=[sys.executable,'-m','unittest','-v'])
    checked=result['command_result']
    assert result['success'] and checked['exit_code']==0
    assert checked['test_summary']['tests_run']==1
    assert 'test_ready' in checked['stderr'] and 'Ran 1 test' in checked['stderr']
    assert checked['stderr'].endswith('OK\n')
    with audit.scoped_root(router.audit_dir(router.registry.get_or_create('a').workspace_id)):
        assert (audit.directory(checked['output_id'])/'stderr').read_text()==checked['stderr']


@pytest.mark.parametrize('arguments',[
    {'host':'a'}, {'host':'a','cwd':'.'}, {'host':'a','cwd':'~/guess'},
    {'host':'a','cwd':'/selected','command':{'commands':[['cat']]},'stdin':'data'},
    {'host':'a','cwd':'/selected','stdin':'x'*1048577},
])
async def test_invalid_inline_inputs_do_not_open_remote_connection(project,monkeypatch,arguments):
    router,server,_,_=project
    def forbidden(*args,**kwargs):pytest.fail('Invalid input reached directory binding')
    monkeypatch.setattr(router,'switch',forbidden)
    with pytest.raises(ToolError):
        await call(server,'a','exec_command',**dict({'command':['pwd']},**arguments))
    assert not router._executors


@pytest.mark.parametrize('command',[
    ['sshpass','-p','sensitive-marker','ssh','host'],['make','-j64'],
])
async def test_authentication_and_heavy_compute_are_checked_before_inline_binding(project,monkeypatch,command):
    router,server,source,_=project
    async def forbidden(*args,**kwargs):pytest.fail('Review refusal contacted the remote')
    monkeypatch.setattr(router,'switch',forbidden)
    result=await call(server,'a','exec_command',host='a',cwd=str(source),command=command)
    assert result['executed'] is False and 'sensitive-marker' not in json.dumps(result)
    assert router.registry.get_or_create('a').host is None and not router._executors


async def test_inline_switch_and_patch_conflict_keep_real_origins_and_skip_checks(project):
    router,server,source,other=project
    await call(server,'a','exec_command',host='a',cwd=str(source),command=['pwd'])
    switched=await call(server,'a','exec_command',host='b',cwd=str(other),command=['pwd'])
    assert switched['host']=='b' and switched['stdout'].strip()==str(other)
    value=await call(server,'a','apply_patch',patch='*** Begin Patch\n*** Update File: value.txt\n@@\n-wrong context\n+bad\n*** End Patch\n',
        command=[sys.executable,'-c','from pathlib import Path;Path("must-not-run").touch()'])
    assert value['error'] and value['command_skipped']=='apply_not_successful'
    assert (source/'value.txt').read_text()=='original\n'
    assert (other/'value.txt').read_text()=='other\n' and not (other/'must-not-run').exists()
