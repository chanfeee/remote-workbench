"""Claim recovery uses submit/ack evidence; local owner death alone is insufficient."""
import asyncio
import json
from contextlib import contextmanager
from pathlib import Path
import sqlite3
import subprocess
import sys
from unittest.mock import AsyncMock
from types import SimpleNamespace

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from remote_workbench.resource_claims import ResourceClaims, mark_dispatch, mark_job_submission
from remote_workbench.tools import commands
from remote_workbench.workspace import bind_tool
from tests.test_session_server import call, project


def record(root, output, state='finished', **extra):
    path = root / 'audit' / 'w' / output / 'record.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({'execution_state': state, **extra}))
    return path


def test_pre_submit_exception_releases_claim_and_marker(tmp_path):
    resources = ResourceClaims(tmp_path)
    claim = resources.acquire('host', '/repo', 'w', 'write_file', tracked=True)
    with pytest.raises(ValueError, match='invalid review'):
        with resources.operation(claim) as operation:
            assert not operation.submitted
            raise ValueError('invalid review')
    assert resources.active() == []
    assert list(resources.markers.iterdir()) == []


def test_live_owner_is_not_reaped_between_finished_steps(tmp_path):
    resources = ResourceClaims(tmp_path)
    claim = resources.acquire('host', '/repo', 'w', 'apply_patch', tracked=True)
    first, second = 'a' * 32, 'b' * 32
    with resources.operation(claim):
        mark_dispatch(first)
        record(tmp_path, first)
        peer = ResourceClaims(tmp_path)
        assert peer.active()[0]['recovery_state'] == 'operation_in_progress'
        with pytest.raises(ValueError, match='busy'):
            peer.acquire('host', '/repo/sub', 'other', 'write_file')
        # A legacy reader has no output ID it could prematurely reap.
        with resources.connect() as db:
            assert db.execute('SELECT output_id FROM claims WHERE claim_id=?', (claim,)).fetchone()[0] is None
        mark_dispatch(second)
        record(tmp_path, second)
        resources.finish(claim, {'output_id': second, 'execution_state': 'finished'}, shell=True)
    assert resources.active() == []


@pytest.mark.parametrize('acknowledged', [False, True])
def test_cancel_only_releases_when_completion_acknowledged(tmp_path, acknowledged):
    resources = ResourceClaims(tmp_path)
    claim = resources.acquire('host', '/repo', 'w', 'exec_command', tracked=True)
    output = 'a' * 32
    with pytest.raises(asyncio.CancelledError):
        with resources.operation(claim):
            mark_dispatch(output)
            record(tmp_path, output, 'cancelled' if acknowledged else 'remote_state_unknown',
                remote_task={'state': 'finished' if acknowledged else 'running',
                             'cleanup_state': 'process_group_signalled' if acknowledged else 'unconfirmed'})
            raise asyncio.CancelledError()
    assert bool(resources.active()) is not acknowledged


def test_legacy_reader_cannot_release_later_finished_output_with_earlier_unknown(tmp_path):
    resources = ResourceClaims(tmp_path)
    claim = resources.acquire('host', '/repo', 'w', 'apply_patch', tracked=True)
    first, last = 'a' * 32, 'b' * 32
    with resources.operation(claim):
        mark_dispatch(first)
        pending = record(tmp_path, first, 'remote_state_unknown')
        mark_dispatch(last)
        record(tmp_path, last)
        resources.finish(claim, {'output_id': last, 'execution_state': 'finished'}, shell=True)
    active = resources.active()
    assert active[0]['output_id'] == first
    assert active[0]['last_dispatch_output_id'] == last
    pending.write_text(json.dumps({'execution_state': 'finished'}))
    assert ResourceClaims(tmp_path).active() == []


def test_launch_completion_is_not_background_job_completion(tmp_path):
    resources = ResourceClaims(tmp_path)
    claim = resources.acquire('host', '/repo', 'w', 'start_job', tracked=True)
    resources.attach_job(claim, 'job')
    launched, collected = 'a' * 32, 'b' * 32
    with resources.operation(claim):
        mark_dispatch(launched)
        record(tmp_path, launched)
        resources.finish(claim, {'job_id': 'job', 'job_output_id': collected,
                                 'execution_state': 'running'}, background=True)
    assert len(resources.active()) == 1
    record(tmp_path, collected, sync_state='complete')
    assert resources.active() == []


def test_background_submit_without_reply_or_output_remains_unknown(tmp_path):
    resources = ResourceClaims(tmp_path)
    claim = resources.acquire('host', '/repo', 'w', 'start_job', tracked=True)
    resources.attach_job(claim, 'job')
    with pytest.raises(TimeoutError):
        with resources.operation(claim) as operation:
            mark_job_submission()
            raise TimeoutError('possible submit with no reply')
    assert operation.submitted and len(resources.active()) == 1
    with pytest.raises(ValueError, match='busy'):
        ResourceClaims(tmp_path).acquire('host', '/repo', 'other', 'write_file')


def test_marker_persistence_failure_prevents_submission_and_releases_local_claim(tmp_path, monkeypatch):
    resources = ResourceClaims(tmp_path)
    claim = resources.acquire('host', '/repo', 'w', 'write_file', tracked=True)
    monkeypatch.setattr(resources, '_write_marker', lambda *args: (_ for _ in ()).throw(OSError('disk full')))
    with pytest.raises(OSError, match='disk full'):
        with resources.operation(claim) as operation:
            mark_dispatch('a' * 32)
    assert not operation.submitted and resources.active() == []


@pytest.mark.parametrize('phase,remaining', [('prepared', False), ('finished', False), ('unknown', True), ('background', True)])
def test_controller_exit_uses_persisted_evidence_not_pid(tmp_path, phase, remaining):
    code = '''import json,os,sys
from pathlib import Path
from remote_workbench.resource_claims import ResourceClaims,mark_dispatch
root=Path(sys.argv[1]);phase=sys.argv[2]
r=ResourceClaims(root);c=r.acquire('host','/repo','w','start_job' if phase=='background' else 'exec_command',tracked=True)
with r.operation(c):
    if phase!='prepared':
        o='a'*32;mark_dispatch(o);p=root/'audit/w'/o/'record.json';p.parent.mkdir(parents=True)
        p.write_text(json.dumps({'execution_state':'remote_state_unknown' if phase=='unknown' else 'finished'}))
    if phase=='background':r.attach_job(c,'job')
    print(c,flush=True);os._exit(0)
'''
    result = subprocess.run([sys.executable, '-c', code, str(tmp_path), phase],
                            capture_output=True, text=True, timeout=10, check=True)
    assert len(result.stdout.strip()) == 32
    assert bool(ResourceClaims(tmp_path).active()) is remaining


async def test_background_task_does_not_keep_mutating_finished_operation_marker(tmp_path):
    resources = ResourceClaims(tmp_path)
    claim = resources.acquire('host', '/repo', 'w', 'start_job', tracked=True)
    gate = asyncio.Event()
    async def inherited():
        await gate.wait()
        mark_dispatch('b' * 32)
    with resources.operation(claim):
        mark_dispatch('a' * 32)
        task = asyncio.create_task(inherited())
        resources.finish(claim, {'execution_state': 'unknown'}, background=True)
    gate.set()
    await task
    assert resources._marker(claim)['outputs'] == ['a' * 32]


async def test_malformed_compute_review_does_not_reserve_directory_or_unknown_job(project):
    router, server, source, _ = project
    await call(server, 'a', 'workspace_switch', host='a', cwd=str(source))
    command = 'printf reviewed'
    with pytest.raises(ToolError, match='requires concurrency'):
        await call(server, 'a', 'start_job', command=command, idempotency_key='review-retry',
                   compute_review={'confirmation': 'incomplete'})
    assert router.resources.active() == []
    state = router.registry.get_or_create('a')
    operation = router.registry.get_job_operation(state.workspace_id, 'review-retry')
    assert operation.state == 'rejected'
    retried = await call(server, 'a', 'start_job', command=command,
                         idempotency_key='review-retry', wait_seconds=2)
    assert retried['operation_state'] == 'submitted' and retried.get('error') is None


async def test_pre_submit_connection_failure_does_not_block_next_write(project, monkeypatch):
    router, server, source, _ = project
    await call(server, 'a', 'workspace_switch', host='a', cwd=str(source))
    executor = router.snapshot(router.registry.get_or_create('a')).workspace.executor
    with monkeypatch.context() as patch:
        patch.setattr(executor, 'check_connection', AsyncMock(return_value=(False, 'Disconnected')))
        with pytest.raises(ToolError, match='Disconnected'):
            await call(server, 'a', 'write_file', path='value.txt', content='not sent')
    assert router.resources.active() == []
    assert (source / 'value.txt').read_text() == 'original\n'
    result = await call(server, 'a', 'write_file', path='value.txt', content='next')
    assert result['success'] and (source / 'value.txt').read_text() == 'next'


@pytest.mark.parametrize('background', [False, True])
def test_completion_commit_failure_keeps_recovery_evidence(tmp_path, monkeypatch, background):
    resources = ResourceClaims(tmp_path)
    claim = resources.acquire('host', '/repo', 'w', 'start_job' if background else 'exec_command', tracked=True)
    launched, collected = 'a' * 32, 'b' * 32
    if background:
        resources.attach_job(claim, 'job')
    original_connect = resources.connect

    @contextmanager
    def failed_commit():
        db = original_connect()
        try:
            yield db
            db.rollback()
            raise sqlite3.OperationalError('injected completion commit failure')
        finally:
            db.close()

    with resources.operation(claim):
        mark_dispatch(launched)
        record(tmp_path, launched)
        if background:
            mark_job_submission()
            record(tmp_path, collected, sync_state='complete')
        result = {'execution_state': 'finished', 'output_id': launched}
        if background:
            result.update(job_id='job', job_output_id=collected)
        with monkeypatch.context() as patch:
            patch.setattr(resources, 'connect', failed_commit)
            with pytest.raises(sqlite3.OperationalError, match='completion commit failure'):
                resources.finish(claim, result, background=background, shell=not background)

    # No database commit means both the row and sidecar must survive. A fresh
    # controller can reconcile acknowledged output without rerunning the task.
    with original_connect() as db:
        assert db.execute('SELECT count(*) FROM claims WHERE claim_id=?', (claim,)).fetchone()[0] == 1
    assert resources._marker(claim) is not None
    assert ResourceClaims(tmp_path).active() == []
    assert list(resources.markers.iterdir()) == []


@pytest.mark.parametrize('damage', ['missing_guard', 'corrupt_marker'])
def test_incomplete_local_tracking_does_not_prove_remote_completion(tmp_path, damage):
    resources = ResourceClaims(tmp_path)
    claim = resources.acquire('host', '/repo', 'w', 'exec_command', tracked=True)
    with pytest.raises(TimeoutError):
        with resources.operation(claim):
            mark_dispatch('a' * 32)
            raise TimeoutError('reply unavailable')
    marker, guard = resources._paths(claim)
    if damage == 'missing_guard':
        guard.unlink()
    else:
        marker.write_text('{incomplete')
    peer = ResourceClaims(tmp_path)
    assert len(peer.active()) == 1
    with pytest.raises(ValueError, match='busy'):
        peer.acquire('host', '/repo/sub', 'other', 'write_file', tracked=True)


def test_other_process_cannot_reap_live_owner_between_acknowledged_steps(tmp_path):
    code = '''import sys,json
from pathlib import Path
from remote_workbench.resource_claims import ResourceClaims,mark_dispatch
root=Path(sys.argv[1]);r=ResourceClaims(root)
c=r.acquire('host','/repo','w','apply_patch',tracked=True)
with r.operation(c):
    o='a'*32;mark_dispatch(o)
    p=root/'audit/w'/o/'record.json';p.parent.mkdir(parents=True)
    p.write_text(json.dumps({'execution_state':'finished'}))
    print(c,flush=True)
    sys.stdin.read(1)
    r.finish(c,{'execution_state':'finished','output_id':o},shell=True)
'''
    process = subprocess.Popen([sys.executable, '-c', code, str(tmp_path)],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True)
    try:
        import select
        assert select.select([process.stdout], [], [], 10)[0], 'controller did not become ready'
        claim = process.stdout.readline().strip()
        assert len(claim) == 32
        peer = ResourceClaims(tmp_path)
        assert peer.active()[0]['recovery_state'] == 'operation_in_progress'
        with pytest.raises(ValueError, match='busy'):
            peer.acquire('host', '/repo/sub', 'other', 'write_file', tracked=True)
    finally:
        _, errors = process.communicate('\n', timeout=10)
    assert process.returncode == 0, errors
    assert peer.active() == []
    assert list(peer.markers.iterdir()) == []


async def test_attach_failure_rejects_job_and_allows_same_key_retry(project, monkeypatch):
    router, server, source, _ = project
    state = await call(server, 'a', 'workspace_switch', host='a', cwd=str(source))
    with monkeypatch.context() as patch:
        def unavailable(*args):
            raise sqlite3.OperationalError('injected job attachment failure')
        patch.setattr(router.resources, 'attach_job', unavailable)
        with pytest.raises(ToolError, match='job attachment failure'):
            await call(server, 'a', 'start_job', command='printf once', idempotency_key='attach-retry')
    assert router.resources.active() == []
    operation = router.registry.get_job_operation(state['workspace_id'], 'attach-retry')
    assert operation.state == 'rejected'
    result = await call(server, 'a', 'start_job', command='printf once',
                        idempotency_key='attach-retry', wait_seconds=2)
    assert result['job_id'] == operation.job_id and result['operation_state'] == 'submitted'
    assert result.get('error') is None


async def test_claim_bookkeeping_failure_preserves_success_and_allows_next_write(project, monkeypatch):
    router, server, source, _ = project
    await call(server, 'a', 'workspace_switch', host='a', cwd=str(source))
    with monkeypatch.context() as patch:
        def unavailable(*args, **kwargs):
            raise sqlite3.OperationalError('injected completion accounting failure')
        patch.setattr(router.resources, 'finish', unavailable)
        result = await call(server, 'a', 'write_file', path='value.txt', content='written once\n')
    assert result['success'] and result['resource_warning'] == 'completion_record_pending'
    assert (source / 'value.txt').read_text() == 'written once\n'
    # A peer reaps only because the real transport record acknowledged the
    # completed write; its metadata failure does not force a replay.
    next_write = await call(server, 'b', 'workspace_switch', host='a', cwd=str(source))
    assert next_write['cwd'] == str(source)
    result = await call(server, 'b', 'write_file', path='other.txt', content='independent\n')
    assert result['success'] and router.resources.active() == []


async def test_fixed_workspace_pre_submit_rejection_releases_claim(project):
    router, server, source, _ = project
    await call(server, 'a', 'workspace_switch', host='a', cwd=str(source))
    workspace = router.snapshot(router.registry.get_or_create('a')).workspace
    session = SimpleNamespace(resources=router.resources, id='fixed-session')
    invoke = bind_tool(workspace, 'start_job', commands.ssh_run_background, 'start', session=session)
    with pytest.raises(ValueError, match='requires concurrency'):
        await invoke(command='printf not-submitted', compute_review={'confirmation': 'incomplete'})
    assert router.resources.active() == []
    assert list(router.resources.markers.iterdir()) == []
