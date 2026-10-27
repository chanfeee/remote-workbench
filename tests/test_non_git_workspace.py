"""Ordinary workspace roots and build/run scripts do not require a Git repo."""
from http.server import ThreadingHTTPServer
from pathlib import Path
import stat
import subprocess
from threading import Thread

import httpx
import pytest

from remote_workbench import audit, remote_worker
from remote_workbench.audit_web import handler_for_directory
from remote_workbench.native_patch import prepared_file_patch
from remote_workbench.remote_directory import inspect_directory
from remote_workbench.session_server import create_server
from remote_workbench.tools.patch import ssh_apply_patch
from tests.test_session_server import call, project, router_at


async def test_plain_workspace_with_git_source_and_run_diff_in_viewer(project):
    router, server, root, _ = project
    source, run, build = root/'source', root/'run', root/'build'
    for path in (source, run, build):
        path.mkdir()
    subprocess.run(['git', 'init', '-q', str(source)], check=True)
    code = source/'code.py'
    code.write_text('VALUE = 1\n')
    script = run/'launch.sh'
    script.write_text('printf "before\\n"\n')
    script.chmod(0o750)

    bound = await call(server, 'a', 'workspace_switch', host='a', cwd=str(root),
                       command=['pwd'], edit_files=['run/launch.sh'])
    assert bound['cwd'] == str(root) and bound['bound']
    assert bound['initial']['stdout'].strip() == str(root)
    ref = bound['files'][0]
    assert ref['edit_id'] and not ref.get('error')
    # The same retained reference remains usable after a controller restart.
    restarted = create_server(router_at(router.state_dir))
    changed = await call(restarted, 'a', 'apply_patch',
                         edits=[{'edit_id':ref['edit_id'], 'new':['printf "after\\n"']}],
                         command=['/bin/sh', 'run/launch.sh'])
    assert changed['success'] and changed['command_result']['stdout'] == 'after\n'
    assert stat.S_IMODE(script.stat().st_mode) == 0o750
    assert code.read_text() == 'VALUE = 1\n'
    assert not (root/'.git').exists() and not (run/'.git').exists() and not (build/'.git').exists()
    assert not list(root.parent.rglob('.remote-workbench-sessions'))

    # Read the exact API used by the optional HTML viewer, without needing Git.
    viewer = ThreadingHTTPServer(('127.0.0.1', 0), handler_for_directory(router.audit_dir(bound['workspace_id'])))
    thread = Thread(target=viewer.serve_forever, daemon=True)
    thread.start()
    try:
        with httpx.Client(base_url=f'http://127.0.0.1:{viewer.server_port}', trust_env=False) as client:
            record = client.get('/api/record', params={'id':changed['output_id']}).json()
            diff = client.get('/api/output', params={'id':changed['output_id'], 'stream':'diff'}).json()
        assert record['patch_result'] == 'applied'
        assert '-printf "before' in diff['content'] and '+printf "after' in diff['content']
        assert 'run/launch.sh' in diff['content']
    finally:
        viewer.shutdown()
        viewer.server_close()
        thread.join()


async def test_edit_explicit_run_directory_outside_selected_workspace(project):
    _, server, root, other = project
    script = other/'run.sh'
    script.write_text('echo before\n')
    await call(server, 'a', 'workspace_switch', host='a', cwd=str(root))
    ref = await call(server, 'a', 'read_file', path=str(script), for_edit=True)
    changed = await call(server, 'a', 'apply_patch', edits=[{'edit_id':ref['edit_id'], 'new':['echo after']}])
    assert changed['success']
    result = await call(server, 'a', 'exec_command', cwd=str(other), command=['/bin/sh', 'run.sh'])
    assert result['stdout'] == 'after\n'
    assert not (root/'.git').exists() and not (other/'.git').exists()


@pytest.mark.parametrize('format', ['context', 'unified'])
async def test_patch_text_in_plain_directory_checks_and_records_diff(local, format):
    _, root = local
    target = root/'run.sh'
    target.write_text('echo before\n')
    text = ('*** Begin Patch\n*** Update File: run.sh\n@@\n-echo before\n+echo after\n*** End Patch\n'
            if format == 'context' else '--- a/run.sh\n+++ b/run.sh\n@@ -1 +1 @@\n-echo before\n+echo after\n')
    checked = await ssh_apply_patch(text, cwd=str(root), check_only=True)
    assert checked.get('success'), checked
    assert target.read_text() == 'echo before\n'
    changed = await ssh_apply_patch(text, cwd=str(root))
    assert changed.get('success'), changed
    assert target.read_text() == 'echo after\n' and not (root/'.git').exists()
    saved = (audit.directory(changed['output_id'])/'diff').read_text()
    assert '-echo before' in saved and '+echo after' in saved


def test_snapshot_edit_needs_no_git_executable_and_still_rejects_conflicts(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv('REMOTE_WORKBENCH_REMOTE_STATE_DIR', str(tmp_path/'state'))
    def forbidden(*args, **kwargs):
        pytest.fail('Snapshot editing must not invoke Git or another subprocess')
    monkeypatch.setattr(remote_worker.subprocess, 'run', forbidden)
    target = tmp_path/'run.sh'
    target.write_text('echo before\n')
    read = remote_worker.read_lines({'path':'run.sh', 'offset':0, 'limit':160, 'budget':8192, 'for_edit':True})
    assert '_edit_data_base64' in read
    before = {str(target):b'echo before\n'}
    after = {str(target):'echo after\n'}
    payload = {'patch':prepared_file_patch(before, after, tmp_path),
               'expected_files':{str(target):read['sha256']},
               'expected_repository':str(tmp_path), 'prepared_contents':after}
    checked = remote_worker.apply_patch(dict(payload, check_only=True))
    assert checked['success'] and target.read_text() == 'echo before\n'
    target.write_text('external edit\n')
    conflict = remote_worker.apply_patch(payload)
    assert conflict['error'] == 'conflict' and target.read_text() == 'external edit\n'
    target.write_text('echo before\n')
    assert remote_worker.apply_patch(payload)['success']
    assert target.read_text() == 'echo after\n'


async def test_directory_binding_does_not_probe_git(tmp_path):
    class Executor:
        async def check_connection(self):
            return True, 'connected'
        async def run_program(self, argv, **kwargs):
            assert 'git' not in argv[2] and 'subprocess' not in argv[2]
            result = subprocess.run(argv, capture_output=True, cwd=kwargs['cwd'], text=True, check=True)
            return type('Result', (), {'stdout':result.stdout, 'exit_code':0})()
    detail = await inspect_directory(Executor(), str(tmp_path))
    assert detail['cwd'] == str(tmp_path.resolve())
    assert not (tmp_path/'.git').exists()


def test_prepared_edit_refuses_git_metadata_even_when_cwd_is_git_directory(tmp_path, monkeypatch):
    metadata = tmp_path/'.git'
    metadata.mkdir()
    (metadata/'config').write_text('protected\n')
    monkeypatch.chdir(metadata)
    result = remote_worker.read_lines({'path':'config', 'offset':0, 'limit':160, 'budget':8192, 'for_edit':True})
    assert result['error'] == 'protected_git_path'
    assert (metadata/'config').read_text() == 'protected\n'
