"""Behavior tests run actual shell/Git/worker code in disposable local directories.

Only the SSH transport is substituted; no configured remote host is contacted.
"""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
from unittest.mock import AsyncMock

import pytest

from remote_workbench import audit
from remote_workbench.config import HostConfig
from remote_workbench.ssh import SSHExecutor
from remote_workbench.tools import files, git, commands, patch




@pytest.mark.asyncio
async def test_cwd_guard_and_home(local):
    executor, path = local
    assert (await executor.run('pwd', cwd='~')).stdout.strip() == str(path)
    sub = path / 'space $(touch nope)'
    sub.mkdir()
    assert (await executor.run('pwd', cwd=str(sub))).stdout.strip() == str(sub)
    result = await executor.run('touch sentinel', cwd=str(path/'absent'))
    assert result.exit_code == 125
    assert not (path/'sentinel').exists()
    record = json.loads((audit.directory(result.output_id)/'record.json').read_text())
    assert record['actual_cwd'] is None


@pytest.mark.asyncio
async def test_budget_and_full_spool(local):
    executor, _ = local
    result = await executor.run("python3 -c 'import sys;sys.stdout.write(\"x\"*300000);sys.stderr.write(\"y\"*200000)'", output_budget=1024)
    assert len(result.stdout.encode())+len(result.stderr.encode()) <= 1024
    assert result.truncated
    assert (audit.directory(result.output_id)/'stdout').stat().st_size == 300000
    assert (audit.directory(result.output_id)/'stderr').stat().st_size == 200000
    cursor, output = 0, ''
    while True:
        page = audit.read_output(result.output_id, 'stdout', cursor, 65536)
        output += page['content']
        cursor = page['next_cursor']
        if not page['truncated']: break
    assert output == 'x'*300000


@pytest.mark.asyncio
async def test_remote_deadline_retains_output_and_cleanup_evidence(local):
    executor, _ = local
    # Include enough time for native shell/Python startup before interrupting
    # the command; a 200 ms startup deadline cannot promise any stdout yet.
    result = await executor.run("printf partial; exec sleep 10", timeout=2)
    assert result.timed_out and result.execution_state == 'finished'
    assert result.remote_task['reason'] == 'timeout'
    assert result.remote_task['cleanup_state'] == 'process_group_signalled'
    assert result.remote_task['tree_confirmed'] is False
    assert (audit.directory(result.output_id)/'stdout').read_text() == 'partial'


@pytest.mark.asyncio
async def test_remote_batch_atomic_hash_mode_unicode(local):
    _, path = local
    target = path/'script.py'
    original = '名字 = 1\r\nlast=2'
    target.write_bytes(original.encode())
    target.chmod(0o751)
    result = await files.ssh_patch_file(str(target), edits=[{'old':'名字 = 1','new':'名字 = 3'},{'old':'last=2','new':'last=4'}], expected_sha256=hashlib.sha256(original.encode()).hexdigest())
    assert result['success']
    assert target.read_bytes() == '名字 = 3\r\nlast=4'.encode()
    assert target.stat().st_mode & 0o777 == 0o751
    assert '名字' in result['diff']
    snapshot=target.read_bytes()
    bad=await files.ssh_patch_file(str(target), edits=[{'old':'last=4','new':'last=5'},{'old':'absent','new':'x'}])
    assert bad['error']=='not_found' and target.read_bytes()==snapshot
    bad=await files.ssh_patch_file(str(target), old='last=4',new='last=5',expected_sha256='0'*64)
    assert bad['error']=='conflict' and target.read_bytes()==snapshot
    dry=await files.ssh_patch_file(str(target),old='last=4',new='last=5',check_only=True)
    assert dry['success'] and target.read_bytes()==snapshot
    op=json.loads((audit.directory(result['operation_id'])/'record.json').read_text())
    child=json.loads((audit.directory(result['output_id'])/'record.json').read_text())
    assert op['tool']=='ssh_patch_file' and child['parent_id']==op['id']
    assert child['actual_cwd']==str(path)
    assert 'python3 -c' in child['wrapped_command']


@pytest.mark.asyncio
async def test_atomic_writes_lines_and_no_truncated_download(local):
    _, path=local
    target=path/'one.txt'
    assert (await files.ssh_write_file(str(target),'a\nb\nc\n'))['success']
    assert (await files.ssh_replace_lines(str(target),2,2,'B\n',expected_old='b\n'))['success']
    assert (await files.ssh_insert_lines(str(target),0,'start\n'))['success']
    assert (await files.ssh_append_file(str(target),'end'))['success']
    assert target.read_text()=='start\na\nB\nc\nend'
    data=await files.ssh_read_file(str(target),offset=1,limit=2)
    assert data['content']=='2\ta\n3\tB'
    assert data['sha256']==hashlib.sha256(target.read_bytes()).hexdigest()
    wrong=await files.ssh_replace_lines(str(target),1,1,'bad',expected_old='wrong')
    assert wrong['error']=='conflict'
    target.write_text('x'*50000)
    # Download path is under home in production; simulated home is set per child.
    import unittest.mock
    with unittest.mock.patch('remote_workbench.tools.files.os.path.expanduser', side_effect=lambda x: str(path) if x=='~' else x):
        result=await files.ssh_download(str(target),local_dir=str(path/'downloads'))
    assert Path(result['local_path']).read_bytes()==target.read_bytes()


def init_repo(path):
    subprocess.run(['git','init','-q',str(path)],check=True)
    subprocess.run(['git','-C',str(path),'add','.'],check=True)
    subprocess.run(['git','-C',str(path),'-c','user.name=Test','-c','user.email=test@local','commit','-qm','base'],check=True)


@pytest.mark.asyncio
async def test_apply_patch_precheck_and_index_unchanged(local):
    _,path=local
    repo=path/'repo';repo.mkdir()
    (repo/'a').write_text('one\n');(repo/'b').write_text('two\n');init_repo(repo)
    text='diff --git a/a b/a\n--- a/a\n+++ b/a\n@@ -1 +1 @@\n-one\n+ONE\n'
    result=await patch.ssh_apply_patch(text,cwd=str(repo),check_only=True)
    assert result['success'] and (repo/'a').read_text()=='one\n'
    invalid=text+'diff --git a/b b/b\n--- a/b\n+++ b/b\n@@ -1 +1 @@\n-NO\n+TWO\n'
    result=await patch.ssh_apply_patch(invalid,cwd=str(repo))
    assert result['error']=='patch_conflict' and (repo/'a').read_text()=='one\n'
    assert (await patch.ssh_apply_patch(text,cwd=str(repo)))['success']
    assert (repo/'a').read_text()=='ONE\n'
    assert subprocess.check_output(['git','-C',str(repo),'diff','--cached'])==b''


@pytest.mark.asyncio
async def test_diff_pages_stable_and_oversized_hunk(local):
    _,path=local
    repo=path/'repo';repo.mkdir()
    target=repo/'a';target.write_text(''.join(f'line {i}\n' for i in range(300)));init_repo(repo)
    text=target.read_text()
    for i in (3,60,120,180,240):text=text.replace(f'line {i}\n',f'CHANGED {i}\n')
    target.write_text(text)
    summary=await git.ssh_git_diff(cwd=str(repo))
    assert '5\t5\ta' in summary['summary']
    page=await git.ssh_git_diff(cwd=str(repo),mode='patch',max_bytes=256)
    record=page['output_id'];collected=page['diff'];cursor=page['next_cursor']
    target.write_text('changed after snapshot')
    while page['truncated']:
        page=await git.ssh_git_diff(mode='patch',output_id=record,cursor=cursor,max_bytes=256)
        assert page['next_cursor']>cursor
        cursor=page['next_cursor'];collected+=page['diff']
    assert collected==(audit.directory(record)/'stdout').read_text()
    large=await git.ssh_git_diff(cwd=str(repo),mode='patch',max_bytes=256)
    if 'diff' in large and large['truncated']:
        large=await git.ssh_git_diff(mode='patch',output_id=large['output_id'],cursor=large['next_cursor'],max_bytes=256)
    assert large['error']=='hunk_exceeds_budget'


@pytest.mark.asyncio
async def test_background_exit_and_incremental_output(local):
    _,path=local
    launch=await commands.ssh_run_background("printf 'first\\n'; printf 'error' >&2; sleep 0.1; printf 'second\\n'; exit 7",cwd=str(path))
    job=launch['job_id']
    cursor=0;errors=0;content='';err=''
    for _ in range(60):
        status=await commands.ssh_job_status(job,stdout_cursor=cursor,stderr_cursor=errors)
        content+=status['stdout_tail'];err+=status['stderr_tail']
        cursor=status['next_stdout_cursor'];errors=status['next_stderr_cursor']
        if not status['running']:break
        await asyncio.sleep(0.05)
    assert status['exit_code']==7 and content=='first\nsecond\n' and err=='error'
    again=await commands.ssh_job_status(job,stdout_cursor=cursor,stderr_cursor=errors)
    assert again['stdout_tail']==again['stderr_tail']==''
    duplicate=await commands.ssh_run_background('true',job_id=job,cwd=str(path))
    assert duplicate['error']=='remote_operation_failed'


@pytest.mark.asyncio
async def test_utf8_pages_and_id_validation(local):
    executor,_=local
    result=await executor.run("printf '中文中文中文'")
    first=audit.read_output(result.output_id,max_bytes=4)
    second=audit.read_output(result.output_id,cursor=first['next_cursor'],max_bytes=65536)
    assert first['content']+second['content']=='中文中文中文'
    with pytest.raises(ValueError):audit.read_output('../outside')
    with pytest.raises(ValueError):audit.read_output(result.output_id,stream='../record.json')


@pytest.mark.asyncio
async def test_optional_bash_trace(local):
    executor,_=local
    result=await executor.run('item=world; printf "%s" "$item"',trace=True)
    assert result.stdout=='world'
    assert 'printf %s world' in result.stderr
    record=json.loads((audit.directory(result.output_id)/'record.json').read_text())
    assert record['shell_trace'] is True


@pytest.mark.asyncio
async def test_read_long_line_explicit_budget_error(local):
    _,path=local
    target=path/'large_line';target.write_text('x'*1000)
    value=await files.ssh_read_file(str(target),max_bytes=256)
    assert value['error']=='line_exceeds_budget' and value['required_bytes']==1002


def test_audit_web_reads_and_rejects_foreign_origin(tmp_path,monkeypatch):
    import threading
    import httpx
    from http.server import ThreadingHTTPServer
    from remote_workbench.audit_web import Handler
    monkeypatch.setenv('REMOTE_WORKBENCH_AUDIT_DIR',str(tmp_path/'audit'))
    record=audit.create(kind='command',command='<script>alert(1)</script>')
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    base=f'http://127.0.0.1:{server.server_port}'
    try:
        with httpx.Client(trust_env=False) as client:
            response=client.get(base+'/')
            assert response.status_code==200 and '操作记录' in response.text
            assert client.get(base+'/api/records').json()[0]['id']==record
            assert client.get(base+'/api/record',params={'id':record}).json()['command']=='<script>alert(1)</script>'
            assert client.get(base+'/api/records',headers={'Origin':'https://outside.test'}).status_code==403
            assert client.get(base+'/api/records',headers={'Host':'outside.test'}).status_code==403
    finally:
        server.shutdown();server.server_close();thread.join()


@pytest.mark.asyncio
async def test_real_stdio_mcp_handshake(tmp_path):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    parameters=StdioServerParameters(command=sys.executable,args=['-m','remote_workbench'],
        env=dict(os.environ,REMOTE_WORKBENCH_AUDIT_DIR=str(tmp_path/'mcp-audit')))
    async with stdio_client(parameters) as streams:
        async with ClientSession(*streams) as session:
            result=await session.initialize()
            assert result.serverInfo.name=='remote-workbench-ssh'
            tools=await session.list_tools()
            assert {'ssh_apply_patch','ssh_audit_list','ssh_read_output'} <= {item.name for item in tools.tools}
            result=await session.call_tool('ssh_audit_list',{'limit':1})
            assert not result.isError


@pytest.mark.asyncio
async def test_noisy_preamble_framed_result_and_concurrent_edits(local):
    executor,path=local
    executor.config.preamble="printf 'environment banner\\n'"
    target=path/'text';target.write_text('one\ntwo\n')
    first,second=await asyncio.gather(
        files.ssh_patch_file(str(target),old='one',new='ONE'),
        files.ssh_patch_file(str(target),old='two',new='TWO'))
    assert first['success'] and second['success']
    assert target.read_text()=='ONE\nTWO\n'
    assert not list(path.glob('.remote-workbench-lock-*'))
    record=audit.directory(first['output_id'])
    assert record.stat().st_mode & 0o777==0o700
    assert (record/'stdout').stat().st_mode & 0o777==0o600


@pytest.mark.asyncio
async def test_git_status_special_paths_and_staged_changes(local):
    _,path=local
    repo=path/'repo';repo.mkdir()
    target=repo/'a file';target.write_text('before\n');init_repo(repo)
    target.write_text('after\n')
    (repo/'新\n文件').write_text('untracked')
    status=await git.ssh_git_status(cwd=str(repo))
    assert status['modified']==['M a file']
    assert status['untracked']==['新\n文件'] and not status['clean']
    subprocess.run(['git','-C',str(repo),'add','a file'],check=True)
    status=await git.ssh_git_status(cwd=str(repo))
    assert status['staged']==['M a file']


@pytest.mark.asyncio
async def test_eof_diff_and_patch_path_escape_rejected(local):
    _,path=local
    target=path/'text';target.write_text('before')
    value=await files.ssh_patch_file(str(target),old='before',new='after')
    assert '-before\n\\ No newline at end of file\n' in value['diff']
    repo=path/'repo';repo.mkdir();(repo/'a').write_text('base\n');init_repo(repo)
    text='diff --git a/../escaped b/../escaped\nnew file mode 100644\n--- /dev/null\n+++ b/../escaped\n@@ -0,0 +1 @@\n+bad\n'
    value=await patch.ssh_apply_patch(text,cwd=str(repo))
    assert value['error']=='patch_conflict' and not (path/'escaped').exists()


@pytest.mark.asyncio
async def test_relative_paths_follow_bound_workspace_and_state_isolated(local):
    executor,path=local
    project=path/'bound-workspace';project.mkdir()
    executor.config.default_cwd=str(project)
    state=project/'private-state'
    executor.config.preamble='export REMOTE_WORKBENCH_REMOTE_STATE_DIR='+shlex.quote(str(state))
    value=await files.ssh_write_file('relative.txt','content')
    assert value['success'] and (project/'relative.txt').read_text()=='content'
    assert not (path/'relative.txt').exists()
    value=await files.ssh_read_file('relative.txt')
    assert value['content']=='1\tcontent'
    assert (state/'locks/writer.lock').is_file()
    launch=await commands.ssh_run_background('exit 0')
    assert Path(launch['job_dir']).is_relative_to(state)
    for _ in range(30):
        status=await commands.ssh_job_status(launch['job_id'])
        if not status['running']:break
        await asyncio.sleep(0.05)
    assert status['exit_code']==0


@pytest.mark.asyncio
async def test_native_zsh_startup_aliases_stdin_background_and_fresh_env(local,monkeypatch):
    import shutil
    zsh=shutil.which('zsh')
    if not zsh:pytest.skip('zsh is not installed')
    executor,path=local
    (path/'native-shell').write_text('#!/bin/sh\nexec '+shlex.quote(zsh)+' -l "$@"\n')
    monkeypatch.setenv('ZDOTDIR',str(path))
    (path/'.zprofile').write_text('export REMOTE_WORKBENCH_TEST_PROFILE=profile\n')
    (path/'.zshrc').write_text('export REMOTE_WORKBENCH_TEST_RC=rc\nalias remote-workbench_native_alias="printf alias-loaded"\nfunction remote-workbench_native_function() { printf function-loaded; }\n')
    with (path/'.zshrc').open('a') as output:
        output.write("printf 'native-init-banner\\n'\nprintf 'native-init-warning\\n' >&2\n")
    value=await executor.run('[[ -o interactive && -o login ]] || exit 91\nprint -r -- "$REMOTE_WORKBENCH_TEST_PROFILE:$REMOTE_WORKBENCH_TEST_RC"\nremote-workbench_native_alias\nremote-workbench_native_function\npython3 -c \'import sys;print(sys.stdin.read())\'',input_data=b'json-stdin')
    assert value.exit_code==0 and 'profile:rc' in value.stdout,value
    assert 'alias-loadedfunction-loadedjson-stdin' in value.stdout
    assert 'native-init-banner' not in value.stdout and 'native-init-warning' not in value.stderr
    assert 'native-init-banner' in audit.read_output(value.output_id,'startup_stdout')['content']
    assert 'native-init-warning' in audit.read_output(value.output_id,'startup_stderr')['content']
    metadata=json.loads((audit.directory(value.output_id)/'record.json').read_text())
    assert metadata['startup_output_bytes']['startup_stdout']>0
    assert (await files.ssh_write_file('native.json','{"value":"中文"}'))['success']
    job=await commands.ssh_run_background('remote-workbench_native_function; exit 6')
    for _ in range(40):
        status=await commands.ssh_job_status(job['job_id'])
        if not status['running']:break
        await asyncio.sleep(0.05)
    assert status['exit_code']==6 and status['stdout_tail']=='function-loaded',status
    assert (await executor.run('export REMOTE_WORKBENCH_EPHEMERAL_ONLY=1')).exit_code==0
    fresh=await executor.run('print -r -- "${REMOTE_WORKBENCH_EPHEMERAL_ONLY-unset}"')
    assert fresh.stdout.strip()=='unset'
