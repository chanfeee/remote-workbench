"""Deterministic work-count/event regressions; no flaky wall-clock thresholds."""
import asyncio
import json
from pathlib import Path
import select
import shlex
import sys
from unittest.mock import AsyncMock

import pytest

from remote_workbench import audit, remote_task, remote_worker
from remote_workbench.config import HostConfig
from tests.test_session_server import call, project


def count_transport(monkeypatch):
    transport = asyncio.create_subprocess_exec
    calls = []
    async def counted(*args, **kwargs):
        calls.append(args)
        return await transport(*args, **kwargs)
    monkeypatch.setattr('remote_workbench.ssh.asyncio.create_subprocess_exec', counted)
    return calls


async def test_repeated_inline_target_keeps_origin_and_needs_only_command_transport(project, monkeypatch):
    router, server, source, _ = project
    await call(server, 'a', 'exec_command', host='a', cwd=str(source), command=['pwd'])
    before = router.registry.get_or_create('a')
    history = router.registry.history(before.workspace_id)
    inspected = AsyncMock(wraps=router.inspect_directory)
    monkeypatch.setattr(router, 'inspect_directory', inspected)
    launches = count_transport(monkeypatch)

    result = await call(server, 'a', 'exec_command', host='a', cwd=str(source), command=['pwd'])

    assert result['stdout'].strip() == str(source)
    assert result['host'] == 'a' and result['cwd'] == str(source)
    assert len(launches) == 1
    inspected.assert_not_awaited()
    after = router.registry.get_or_create('a')
    assert (after.target_id, after.revision) == (before.target_id, before.revision)
    assert router.registry.history(after.workspace_id) == history
    with audit.scoped_root(router.audit_dir(after.workspace_id)):
        record = json.loads((audit.directory(result['output_id'])/'record.json').read_text())
        assert record['target_id'] == before.target_id and record['actual_cwd'] == str(source)


@pytest.mark.parametrize('different_host', [False, True])
async def test_changed_inline_target_still_validates(project, monkeypatch, different_host):
    router, server, source, other = project
    await call(server, 'a', 'exec_command', host='a', cwd=str(source), command=['pwd'])
    inspected = AsyncMock(wraps=router.inspect_directory)
    monkeypatch.setattr(router, 'inspect_directory', inspected)
    launches = count_transport(monkeypatch)
    host = 'b' if different_host else 'a'

    result = await call(server, 'a', 'exec_command', host=host, cwd=str(other), command=['pwd'])

    inspected.assert_awaited_once_with(host, str(other))
    assert len(launches) == 2
    assert result['host'] == host and result['stdout'].strip() == str(other)


async def test_alias_and_explicit_switch_still_revalidate_canonical_directory(project, monkeypatch):
    router, server, source, _ = project
    await call(server, 'a', 'exec_command', host='a', cwd=str(source), command=['pwd'])
    inspected = AsyncMock(wraps=router.inspect_directory)
    monkeypatch.setattr(router, 'inspect_directory', inspected)
    alias = str(source) + '/.'

    result = await call(server, 'a', 'exec_command', host='a', cwd=alias, command=['pwd'])
    assert result['cwd'] == str(source)
    inspected.assert_awaited_once_with('a', alias)
    await call(server, 'a', 'workspace_switch', host='a', cwd=str(source))
    assert inspected.await_args_list[-1].args == ('a', str(source))
    assert inspected.await_count == 2


async def test_repeated_inline_target_still_refuses_disconnected_master(project, monkeypatch):
    from mcp.server.fastmcp.exceptions import ToolError
    from remote_workbench.ssh import SSHExecutor
    router, server, source, _ = project
    await call(server, 'a', 'exec_command', host='a', cwd=str(source), command=['pwd'])
    monkeypatch.setattr(SSHExecutor, 'check_connection', AsyncMock(return_value=(False, 'master unavailable')))
    launches = count_transport(monkeypatch)
    with pytest.raises(ToolError, match='master unavailable'):
        await call(server, 'a', 'exec_command', host='a', cwd=str(source), command=['pwd'])
    assert launches == []
    assert router.registry.get_or_create('a').cwd == str(source)


@pytest.mark.parametrize('command', [['make', '-j64'], ['sshpass', '-p', 'must-not-run', 'ssh', 'host']])
async def test_repeated_inline_target_does_not_skip_compute_or_auth_review(project, monkeypatch, command):
    _, server, source, _ = project
    await call(server, 'a', 'exec_command', host='a', cwd=str(source), command=['pwd'])
    launches = count_transport(monkeypatch)
    result = await call(server, 'a', 'exec_command', host='a', cwd=str(source), command=command)
    assert result['executed'] is False and launches == []
    assert 'must-not-run' not in json.dumps(result)


async def test_selected_reads_use_one_worker_with_distinct_recoverable_snapshots(project, monkeypatch):
    router, server, source, other = project
    (source/'second.py').write_text('SECOND = 1\n')
    await call(server, 'a', 'workspace_switch', host='b', cwd=str(other))
    launches = count_transport(monkeypatch)
    bound = await call(server, 'a', 'workspace_switch', host='a', cwd=str(source),
                       read_files=['missing.txt'], edit_files=['value.txt', 'second.py'])
    # One directory validation plus one worker for the entire explicit selection.
    assert len(launches) == 2
    failed, first, second = bound['files']
    assert failed['error'] and first['edit_id'] != second['edit_id']
    assert first['content'] == '1\toriginal' and second['content'] == '1\tSECOND = 1'
    with audit.scoped_root(router.audit_dir(bound['workspace_id'])):
        records = [json.loads((audit.directory(item['output_id'])/'record.json').read_text())
                   for item in bound['files']]
        assert len({record['source_output_id'] for record in records}) == 1
        assert all(record['target_id'] == bound['target_id'] and record['client_session_id'] == 'test-transport'
                   for record in records)
        from remote_workbench.presentation import summarize_record
        assert all(record['tool'] == 'ssh_read_file' for record in records)
        assert 'value.txt' in summarize_record(records[1])['title']
        assert failed['message'] in (audit.directory(failed['output_id'])/'stdout').read_text()
        assert all((audit.directory(item['edit_id'])/'edit/manifest.json').exists() for item in (first, second))
    result = await call(server, 'a', 'apply_patch', edits=[
        {'edit_id':first['edit_id'], 'new':['updated']},
        {'edit_id':second['edit_id'], 'new':['SECOND = 2']}])
    assert result['success']
    assert (source/'value.txt').read_text() == 'updated\n'
    assert (source/'second.py').read_text() == 'SECOND = 2\n'
    foreign = await call(server, 'b', 'workspace_switch', host='a', cwd=str(source))
    assert foreign['workspace_id'] != bound['workspace_id']
    from mcp.server.fastmcp.exceptions import ToolError
    with pytest.raises(ToolError, match='Unknown edit_id'):
        await call(server, 'b', 'apply_patch', edits=[{'edit_id':first['edit_id'], 'new':['blocked']}])


def test_selected_snapshot_budget_is_per_batch_and_keeps_other_files(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for i in range(3):
        (tmp_path/f'{i}.py').write_text('1234567\n')
    value = remote_worker.read_selected({'files':[{'path':f'{i}.py', 'for_edit':True} for i in range(3)],
        'max_file_size':8, 'offset':0, 'limit':160, 'budget':256})
    first, second, third = value['files']
    assert first['_edit_data_base64'] and second['_edit_data_base64']
    assert third['error'] == 'snapshot_batch_budget' and '_edit_data_base64' not in third


def test_batch_read_rejects_managed_copy_without_opening_it(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    value = remote_worker.read_selected({'files':[{'path':'.remote-workbench-sessions/old/project/run.sh', 'for_edit':False}],
        'offset':0, 'limit':160, 'budget':256})
    assert value['files'][0]['error'] == 'read_failed'
    assert 'managed project copy' in value['files'][0]['message']


@pytest.mark.parametrize('selection', [[], [{'path':'x', 'for_edit':'true'}], [{'path':'', 'for_edit':True}]])
def test_bad_selection_fails_before_any_file_is_read(selection, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('Invalid selection reached a file read')
    monkeypatch.setattr(remote_worker, 'read_lines', forbidden)
    with pytest.raises(ValueError, match='explicit files'):
        remote_worker.read_selected({'files':selection})


def test_normal_completion_wakes_supervisor_without_waiting_for_poll_deadline(tmp_path, monkeypatch, capfdbinary):
    monkeypatch.setenv('REMOTE_WORKBENCH_LOGIN_SHELL', '/bin/sh')
    real_select = select.select
    notifications = []
    gate = tmp_path/"finish-workload"
    def observed(reads, writes, errors, timeout=None):
        # Deliberately stretch the control poll; a completion event must wake it.
        observed_wait = timeout == 0.05 and any(fd != 0 for fd in reads)
        if observed_wait:
            gate.touch()
        ready = real_select(reads, writes, errors, 0.5 if observed_wait else timeout)
        if observed_wait:
            notifications.extend(fd for fd in ready[0] if fd != 0)
        return ready
    monkeypatch.setattr(remote_task.select, 'select', observed)
    command = shlex.join([sys.executable, '-c',
        "import time;from pathlib import Path;p=Path(" + repr(str(gate)) + ");end=time.monotonic()+4\n"
        "while not p.exists() and time.monotonic()<end: time.sleep(0.005)\n"
        "assert p.exists();print('complete',end='')"])
    code = remote_task.supervise_task({'task_id':'completion-event' , 'task_dir':str(tmp_path/'task'),
        'cwd':str(tmp_path), 'command':command, 'timeout':5, 'detached':True,
        'markers':{'stdout':'ready-out\n', 'stderr':'ready-err:%s\n'},
        'policy':HostConfig('local-only', execution_enabled=True, terminate_grace_seconds=1).task_policy()})
    assert code == 0 and notifications
    status = json.loads((tmp_path/'task/status.json').read_text())
    assert status['state'] == 'finished' and status['cleanup_state'] != 'unconfirmed'
    assert capfdbinary.readouterr().out == b'complete'


async def test_primary_read_file_list_is_one_call_one_ssh_and_keeps_per_file_bounds(project, monkeypatch):
    router, server, source, _ = project
    await call(server, 'a', 'workspace_switch', host='a', cwd=str(source))
    (source/'second.py').write_text('first\nsecond\nthird\n')
    launches = count_transport(monkeypatch)
    result = await call(server, 'a', 'read_file', path=['value.txt', 'second.py', 'missing.py'],
                        for_edit=True, offset=1, limit=1)
    assert len(launches) == 1
    first, second, missing = result['files']
    assert first['content'] == '' and first['edit_id']
    assert second['content'] == '2\tsecond' and second['edit_id'] != first['edit_id']
    assert missing['error'] == 'file_not_found'
    assert first['requested_path'] == 'value.txt' and second['requested_path'] == 'second.py'
    updated = await call(server, 'a', 'apply_patch', edits=[{'edit_id':second['edit_id'], 'new':['updated']}])
    assert updated['success'] and (source/'second.py').read_text() == 'updated\n'


@pytest.mark.parametrize('paths', [[], ['value.txt','value.txt'], ['value.txt']*17])
async def test_invalid_primary_path_list_stops_before_transport(project, monkeypatch, paths):
    _, server, source, _ = project
    await call(server, 'a', 'workspace_switch', host='a', cwd=str(source))
    launches = count_transport(monkeypatch)
    from mcp.server.fastmcp.exceptions import ToolError
    with pytest.raises(ToolError, match='explicit paths'):
        await call(server, 'a', 'read_file', path=paths, for_edit=True)
    assert launches == []
