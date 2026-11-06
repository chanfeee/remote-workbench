"""Per-file coordination with bounded local processes, never a real cluster."""
import asyncio
from contextlib import contextmanager, ExitStack
import hashlib
import json
import os
from pathlib import Path
import select
import shlex
import subprocess
import sys

import pytest

from remote_workbench import job_collector
from remote_workbench.resource_claims import ResourceClaims
from tests.test_session_server import call, project


ROOT = Path(__file__).resolve().parents[1]
HOLDER = '''
import sys
from pathlib import Path
from remote_workbench.remote_worker import lock
path = Path(sys.argv[1])
with lock(path):
    print('locked', flush=True)
    if sys.stdin.readline().strip() == 'change':
        path.write_text('external\\n')
'''
EDITOR = '''
import json, sys
from contextlib import contextmanager
from remote_workbench import remote_worker as worker
request = json.loads(sys.argv[1])
real_lock = worker.lock
@contextmanager
def traced(paths):
    print('attempt', flush=True)
    with real_lock(paths) as held:
        yield held
worker.lock = traced
try:
    result = worker.apply_patch(request) if request['op'] == 'apply_patch' else worker.edit(request)
except (ValueError, OSError) as exc:
    result = {'error': str(exc)}
print(json.dumps(result), flush=True)
'''


@contextmanager
def child(code, root, argument, target='one', pass_fds=()):
    env = dict(os.environ, HOME=str(root), PYTHONPATH=str(ROOT),
               REMOTE_WORKBENCH_REMOTE_STATE_DIR=str(root / 'targets' / target))
    process = subprocess.Popen([sys.executable, '-u', '-c', code, argument], cwd=root, env=env,
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True, pass_fds=pass_fds)
    try:
        yield process
    finally:
        if process.poll() is None:
            try:
                process.communicate('\n', timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate(timeout=5)


def line(process):
    assert select.select([process.stdout], [], [], 5)[0], 'worker did not reach its barrier'
    value = process.stdout.readline().strip()
    assert value, process.stderr.read() if process.poll() is not None else 'worker closed stdout'
    return value


def request_for(kind, root, name):
    if kind == 'write':
        return {'op': 'write', 'path': str(root / name), 'content': 'after\n'}
    if kind == 'native':
        return {'op': 'apply_patch', 'patch': f'*** Begin Patch\n*** Update File: {name}\n@@\n-before\n+after\n*** End Patch'}
    patch = f'diff --git a/{name} b/{name}\n--- a/{name}\n+++ b/{name}\n@@ -1 +1 @@\n-before\n+after\n'
    if kind == 'unified':
        return {'op': 'apply_patch', 'patch': patch}
    from remote_workbench.native_patch import prepared_file_patch
    path = str(root / name)
    contents = {path: 'after\n'}
    return {'op': 'apply_patch', 'patch': prepared_file_patch({path: b'before\n'}, contents, root),
            'expected_files': {path: hashlib.sha256(b'before\n').hexdigest()},
            'expected_repository': str(root), 'prepared_contents': contents}


@pytest.mark.parametrize('kind', ['write', 'native', 'unified', 'prepared'])
def test_same_file_serializes_but_sibling_file_proceeds_across_targets(tmp_path, kind):
    root = tmp_path.resolve()
    for name in ('a.txt', 'b.txt'):
        (root / name).write_text('before\n')
    with child(HOLDER, root, str(root / 'a.txt'), 'holder') as holder:
        assert line(holder) == 'locked'
        with child(EDITOR, root, json.dumps(request_for(kind, root, 'a.txt')), 'same') as same:
            assert line(same) == 'attempt'
            assert not select.select([same.stdout], [], [], 0.05)[0]
            with child(EDITOR, root, json.dumps(request_for(kind, root, 'b.txt')), 'sibling') as sibling:
                assert line(sibling) == 'attempt'
                assert json.loads(line(sibling))['success'] is True
                sibling.wait(timeout=5)
                assert sibling.returncode == 0
            assert (root / 'a.txt').read_text() == 'before\n'
            assert (root / 'b.txt').read_text() == 'after\n'
            holder.communicate('\n', timeout=5)
            assert json.loads(line(same))['success'] is True
            same.wait(timeout=5)
            assert (root / 'a.txt').read_text() == 'after\n'


def test_native_context_is_read_after_acquiring_file_lock(tmp_path):
    root = tmp_path.resolve()
    path = root / 'a.txt'
    path.write_text('before\n')
    with child(HOLDER, root, str(path), 'holder') as holder:
        assert line(holder) == 'locked'
        with child(EDITOR, root, json.dumps(request_for('native', root, 'a.txt')), 'editor') as editor:
            assert line(editor) == 'attempt'
            holder.communicate('change\n', timeout=5)
            result = json.loads(line(editor))
            editor.wait(timeout=5)
            assert 'context' in result['error']
            assert path.read_text() == 'external\n'


def test_worker_exit_releases_same_file_lock_without_deleting_lock_file(tmp_path):
    root = tmp_path.resolve()
    path = root / 'a.txt'
    path.write_text('before\n')
    with child(HOLDER, root, str(path), 'holder') as holder:
        assert line(holder) == 'locked'
        holder.kill()
        holder.wait(timeout=5)
    assert list((root / '.cache/remote-workbench-ssh/file-locks').glob('*.lock'))
    with child(EDITOR, root, json.dumps(request_for('write', root, 'a.txt')), 'next') as editor:
        assert line(editor) == 'attempt'
        assert json.loads(line(editor))['success'] is True
        editor.wait(timeout=5)
    assert path.read_text() == 'after\n'


def test_mutator_retains_lock_after_controller_closes_its_descriptors(tmp_path, monkeypatch):
    from remote_workbench.remote_worker import lock
    root = tmp_path.resolve()
    monkeypatch.setenv('HOME', str(root))
    path = root / 'a.txt'
    path.write_text('before\n')
    with ExitStack() as stack:
        with lock(path) as held:
            mutator = stack.enter_context(child(
                "import sys; print('mutator', flush=True); sys.stdin.readline()", root, '', pass_fds=held))
            assert line(mutator) == 'mutator'
        # The controller released its descriptors; the actual mutator still
        # holds the same open-file description, as a Git apply child does.
        with child(EDITOR, root, json.dumps(request_for('write', root, 'a.txt')), 'next') as editor:
            assert line(editor) == 'attempt'
            assert not select.select([editor.stdout], [], [], 0.05)[0]
            mutator.communicate('\n', timeout=5)
            assert json.loads(line(editor))['success'] is True
            editor.wait(timeout=5)
    assert path.read_text() == 'after\n'


@pytest.mark.parametrize('native', [True, False])
async def test_patch_paths_are_relative_to_selected_git_subdirectory(project, native):
    _, server, source, _ = project
    subprocess.run(['git', 'init', '-q', str(source)], check=True)
    selected = source / 'sub'
    selected.mkdir()
    (selected / 'value.txt').write_text('before\n')
    await call(server, 'patch-cwd', 'workspace_switch', host='a', cwd=str(selected))
    request = request_for('native' if native else 'unified', selected, 'value.txt')
    result = await call(server, 'patch-cwd', 'apply_patch', patch=request['patch'])
    assert result.get('success'), result
    assert (selected / 'value.txt').read_text() == 'after\n'
    assert (source / 'value.txt').read_text() == 'original\n'


async def test_shared_directory_job_and_unknown_legacy_claim_do_not_block_edits(project):
    router, server, source, _ = project
    a = await call(server, 'a', 'workspace_switch', host='a', cwd=str(source))
    await call(server, 'b', 'workspace_switch', host='a', cwd=str(source))
    claim = router.resources.acquire('host-a', str(source), a['workspace_id'], 'start_job')
    router.resources.finish(claim, {'execution_state': 'unknown'}, background=True)
    command = shlex.join([sys.executable, '-c', "import pathlib,time; p=pathlib.Path('release');\nwhile not p.exists(): time.sleep(0.05)"])
    executor = router.snapshot(router.registry.get(a['workspace_id'])).workspace.executor
    try:
        job = await call(server, 'a', 'start_job', command=command, timeout=10, idempotency_key='shared-directory')
        assert job.get('job_id') and not job.get('error'), job
        queried = await call(server, 'a', 'job_status', job_id=job['job_id'])
        assert queried.get('running') is True, queried
        executed = await call(server, 'b', 'exec_command', command='printf shared')
        assert executed['exit_code'] == 0 and executed['stdout'] == 'shared'
        edited = await call(server, 'b', 'write_file', path='value.txt', content='shared edit\n')
        assert edited['success'] and (source / 'value.txt').read_text() == 'shared edit\n'
        assert [row['claim_id'] for row in router.resources.active()] == [claim]
        info = await call(server, 'b', 'workspace_info')
        assert info['write_coordination'] == 'managed-files'
        (source / 'release').touch()
        waited = await call(server, 'a', 'job_wait', job_id=job['job_id'], wait_seconds=5)
        assert waited.get('exit_code') == 0, waited
    finally:
        (source / 'release').touch()
        await job_collector.close(executor)


async def test_normal_execution_and_recovery_never_open_legacy_claim_store(project, monkeypatch):
    router, server, source, _ = project
    def forbidden(*args, **kwargs):
        raise AssertionError('normal operations must not open the legacy directory store')
    monkeypatch.setattr(ResourceClaims, '__init__', forbidden)
    state = await call(server, 'a', 'workspace_switch', host='a', cwd=str(source))
    executor = router.snapshot(router.registry.get(state['workspace_id'])).workspace.executor
    try:
        assert (await call(server, 'a', 'workspace_info'))['write_coordination'] == 'managed-files'
        assert (await call(server, 'a', 'exec_command', command='printf normal'))['exit_code'] == 0
        job = await call(server, 'a', 'start_job', command='printf once',
                         idempotency_key='no-directory-store', wait_seconds=2)
        recovered = await call(server, 'a', 'resolve_operation', idempotency_key='no-directory-store')
        assert recovered['job_id'] == job['job_id']
        assert recovered.get('local_job', {}).get('job_output_id')
        assert router._resources is None
        assert not (router.state_dir / 'resources.sqlite3').exists()
    finally:
        await job_collector.close(executor)


@pytest.mark.parametrize('native', [True, False])
def test_rename_locks_the_source_as_well_as_destination(tmp_path, native):
    root = tmp_path.resolve()
    source = root / 'old-é.txt'
    source.write_text('before\n')
    if native:
        patch = '*** Begin Patch\n*** Update File: old-é.txt\n*** Move to: new-é.txt\n@@\n-before\n+after\n*** End Patch'
    else:
        patch = ('diff --git "a/old-\\303\\251.txt" "b/new-\\303\\251.txt"\n'
                 'similarity index 100%\nrename from "old-\\303\\251.txt"\nrename to "new-\\303\\251.txt"\n')
    request = {'op': 'apply_patch', 'patch': patch}
    with child(HOLDER, root, str(source), 'holder') as holder:
        assert line(holder) == 'locked'
        with child(EDITOR, root, json.dumps(request), 'rename') as editor:
            assert line(editor) == 'attempt'
            assert not select.select([editor.stdout], [], [], 0.05)[0]
            assert source.exists() and not (root / 'new-é.txt').exists()
            holder.communicate('\n', timeout=5)
            assert json.loads(line(editor))['success'] is True
            editor.wait(timeout=5)
    assert not source.exists()
    assert (root / 'new-é.txt').read_text() == ('after\n' if native else 'before\n')
