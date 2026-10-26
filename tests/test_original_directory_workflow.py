"""Selected roots, code/environment paths and result directories are independent."""
import hashlib,json,shlex,subprocess,sys
from pathlib import Path
from unittest.mock import AsyncMock
import pytest
from mcp.server.fastmcp.exceptions import ToolError
from remote_workbench import workspace_session
from remote_workbench.ssh import SSHExecutor
from remote_workbench.workspace import Workspace,create_server as create_fixed
from remote_workbench.session_server import create_server
from tests.test_session_server import call,project,router_at


async def test_arbitrary_code_environment_and_run_paths_without_copy(project,monkeypatch):
    _,server,root,external=project
    code_dir=root/'program sources';code_dir.mkdir()
    env_dir=root/'settings';env_dir.mkdir()
    env=env_dir/'activate';env.write_text('export WORKFLOW_FACTOR=7\n')
    program=code_dir/'run.py'
    program.write_text('import os,json,sys\nfrom pathlib import Path\nPath(sys.argv[1]).write_text(json.dumps({"cwd":os.getcwd(),"code":__file__,"factor":int(os.environ["WORKFLOW_FACTOR"])}))\n')
    before=hashlib.sha256(program.read_bytes()).hexdigest()
    internal=root/'outputs/run area';internal.mkdir(parents=True)
    await call(server,'a','workspace_switch',host='a',cwd=str(root))
    for cwd in [internal,external]:
        output=cwd/'result.json'
        command=['/bin/sh','-c','. '+shlex.quote(str(env))+'; exec '+shlex.join([sys.executable,str(program),str(output)])]
        value=await call(server,'a','exec_command',cwd=str(cwd),command=command)
        assert value['exit_code']==0
        assert json.loads(output.read_text())=={'cwd':str(cwd),'code':str(program),'factor':7}
    fresh=await call(server,'a','exec_command',command=[sys.executable,'-c','import os;assert "WORKFLOW_FACTOR" not in os.environ'])
    assert fresh['exit_code']==0 and hashlib.sha256(program.read_bytes()).hexdigest()==before
    assert not list(root.parent.rglob('.remote-workbench-sessions'))




async def test_direct_rejects_copy_path_and_symlink_alias(project):
    router,server,source,external=project
    backup=source.parent/'.remote-workbench-sessions/demo/project';backup.mkdir(parents=True)
    alias=source/'alias';alias.symlink_to(backup,target_is_directory=True)
    await call(server,'a','workspace_switch',host='a',cwd=str(source))
    with pytest.raises(ToolError,match='managed project copy'):
        await call(server,'a','workspace_switch',host='a',cwd=str(alias))
    with pytest.raises(ToolError,match='managed project copy'):
        await call(server,'a','exec_command',cwd=str(backup),command=['pwd'])
    # The remote supervisor checks physical cwd too, with no extra SSH probe.
    result=await call(server,'a','exec_command',cwd=str(alias),command=[sys.executable,'-c','from pathlib import Path;Path("sentinel").touch()'])
    assert result['exit_code']!=0 and not (backup/'sentinel').exists()


async def test_fixed_entry_default_keeps_code_shared_and_records_separate(local,monkeypatch):
    executor,tmp=local
    monkeypatch.setattr(SSHExecutor,'check_connection',AsyncMock(return_value=(True,'connected')))
    root=tmp/'existing project';root.mkdir();(root/'value.txt').write_text('original\n')
    template=Workspace('test','host',str(root),executor)
    sessions=[workspace_session.WorkspaceSession(template) for _ in range(2)]
    assert sessions[0].workspace.cwd==sessions[1].workspace.cwd==str(root)
    assert sessions[0].audit_dir!=sessions[1].audit_dir
    servers=[create_fixed(template,session=s) for s in sessions]
    for s in servers:
        info=json.loads((await s.call_tool('workspace_info',{}))[0].text)
        assert info['cwd']==str(root) and not info['project_isolated']
    await sessions[0].ensure_ready()
    assert not Path(sessions[0].workspace.executor.remote_state_dir).is_relative_to(root)
    value=json.loads((await servers[0].call_tool('exec_command',{'command':['pwd']}))[0].text)
    assert value['exit_code']==0 and value['stdout'].strip()==str(root)
    assert not list(tmp.rglob('.remote-workbench-sessions'))
    claim=sessions[0].resources.acquire('local-simulated',str(root),sessions[0].id,'test')
    with pytest.raises(ToolError,match='busy or completion unknown'):
        await servers[1].call_tool('write_file',{'path':'value.txt','content':'blocked'})
    assert (root/'value.txt').read_text()=='original\n'
    sessions[0].resources.finish(claim,{'executed':False})





async def test_copy_interfaces_are_unavailable_without_contacting_ssh(tmp_path):
    router=router_at(tmp_path/'state')
    server=create_server(router)
    for name in ('workspace_changes','workspace_preview','workspace_apply'):
        with pytest.raises(ToolError,match='Unknown|unknown|not found'):
            await call(server,'a','workspace_tools',tool='describe',arguments={'name':name})
    with pytest.raises(ToolError,match='directory_mode'):
        await call(server,'a','workspace_switch',host='a',cwd='/srv/project',directory_mode='copy')
    assert router._executors=={}


async def test_old_copy_binding_and_job_are_not_recovered_or_modified(tmp_path):
    import sqlite3
    from remote_workbench.workspace_registry import WorkspaceTarget
    router=router_at(tmp_path/'state')
    original=tmp_path/'source';original.mkdir()
    copy=tmp_path/'.remote-workbench-sessions/old/project';copy.mkdir(parents=True)
    (copy/'unpublished.txt').write_text('unique work\n')
    state=router.registry.get_or_create('old-chat')
    target=WorkspaceTarget('old-target',state.workspace_id,'a',str(original),str(copy),str(copy))
    # Persist the shape of an old registry without any direct-target record.
    with sqlite3.connect(router.registry.path) as db:
        db.execute('UPDATE workspaces SET target_id=?,host=?,source_cwd=?,cwd=?,project_root=? WHERE workspace_id=?',
                   (target.target_id,target.host,target.source_cwd,target.cwd,target.project_root,state.workspace_id))
    router.registry.reserve_job_operation(state.workspace_id,'old-request',target,'old-job','fingerprint',
        origin_nonce='a'*32,launch_cwd=str(copy),remote_state_dir=str(copy/'.remote-workbench-state'))
    restarted=router_at(router.state_dir)
    server=create_server(restarted)
    info=await call(server,'old-chat','workspace_info')
    assert info['directory_selection_required'] is True
    for name,args in [('exec_command',{'command':['pwd']}),('job_status',{'job_id':'old-job'}),
                      ('stop_job',{'job_id':'old-job'})]:
        with pytest.raises(ToolError,match='managed project copy'):
            await call(server,'old-chat',name,**args)
    recovered=await call(server,'old-chat','resolve_operation',idempotency_key='old-request')
    assert recovered['execution_state']=='unknown'
    assert 'managed project copy' in recovered['state_reason']
    resumed=await call(server,'old-chat','workspace_resume')
    assert resumed['remote_query_performed'] is False
    assert restarted._executors=={}
    assert (copy/'unpublished.txt').read_text()=='unique work\n'
    assert restarted.registry.get_job(state.workspace_id,'old-job')==target
