"""Fewer corrective calls without hiding failed checks or reading unknown tasks."""
import json
import sys
from unittest.mock import AsyncMock

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from remote_workbench import audit
from tests.test_execution_latency import count_transport
from tests.test_session_server import call, project


PATCH = '*** Begin Patch\n*** Update File: value.txt\n@@\n-original\n+changed\n*** End Patch\n'


async def test_failing_baseline_and_fix_need_two_calls_with_original_evidence(project):
    router, server, source, _ = project
    (source / 'logic.py').write_text('def answer():\n    return 1\n')
    (source / 'test_logic.py').write_text('import unittest\nfrom logic import answer\n'
        'class Tests(unittest.TestCase):\n    def test_answer(self):\n        self.assertEqual(answer(), 2)\n')
    first = await call(server, 'a', 'exec_command', host='a', cwd=str(source),
        command=[sys.executable, '-B', '-m', 'unittest', '-v'], edit_files=['logic.py'], read_files=['test_logic.py'])
    assert first['exit_code'] == 1 and 'FAILED' in first['stderr']
    assert len(first['files']) == 2
    second = await call(server, 'a', 'apply_patch',
        edits=[{'edit_id': first['files'][1]['edit_id'], 'new': ['def answer():', '    return 2']}],
        command=[sys.executable, '-B', '-m', 'unittest', '-v'])
    assert second['success'] and second['command_result']['exit_code'] == 0
    assert 'Ran 1 test' in second['command_result']['stderr']
    with audit.scoped_root(router.audit_dir(router.registry.get_or_create('a').workspace_id)):
        assert (audit.directory(first['output_id']) / 'stderr').read_text() == first['stderr']
        assert '+    return 2' in (audit.directory(second['output_id']) / 'diff').read_text()
    assert not router.resources.active()


async def test_patch_check_and_fresh_snapshots_return_post_command_content_even_if_check_fails(project):
    router, server, source, _ = project
    await call(server, 'a', 'exec_command', host='a', cwd=str(source), command=['pwd'])
    result = await call(server, 'a', 'apply_patch', patch=PATCH,
        command=[sys.executable, '-c', 'from pathlib import Path;Path("value.txt").write_text("formatted\\n");raise SystemExit(3)'],
        edit_files=['value.txt'])
    assert result['success'] and result['command_result']['exit_code'] == 3
    selected = result['files'][0]
    assert selected['content'] == '1\tformatted' and selected['edit_id']
    again = await call(server, 'a', 'apply_patch', edits=[{'edit_id': selected['edit_id'], 'new': ['final']}])
    assert again['success'] and (source / 'value.txt').read_text() == 'final\n'
    with audit.scoped_root(router.audit_dir(router.registry.get_or_create('a').workspace_id)):
        record = json.loads((audit.directory(selected['output_id']) / 'record.json').read_text())
        assert record['parent_id'] == result['operation_id'] and record['cwd'] == str(source)


async def test_post_patch_file_paths_use_command_subdirectory_and_keep_binding(project):
    router, server, source, _ = project
    run = source / 'run'
    run.mkdir()
    (run / 'value.txt').write_text('original\n')
    await call(server, 'a', 'exec_command', host='a', cwd=str(source), command=['pwd'])
    result = await call(server, 'a', 'apply_patch', cwd='run', patch=PATCH, command=['pwd'], edit_files=['value.txt'])
    assert result['command_result']['stdout'].strip() == str(run)
    assert result['files'][0]['content'] == '1\tchanged'
    assert router.registry.get_or_create('a').cwd == str(source)
    changed = await call(server, 'a', 'apply_patch',
        edits=[{'edit_id': result['files'][0]['edit_id'], 'new': ['second']}])
    assert changed['success'] and (run / 'value.txt').read_text() == 'second\n'
    assert (source / 'value.txt').read_text() == 'original\n'


@pytest.mark.parametrize('outcome', [
    {'exit_code': 0, 'execution_state': 'unknown'},
    {'exit_code': 0, 'execution_state': 'finished', 'timed_out': True},
    {'executed': False, 'exit_code': 0, 'execution_state': 'finished'},
])
async def test_uncertain_following_command_never_triggers_requested_reads(project, monkeypatch, outcome):
    _, server, source, _ = project
    await call(server, 'a', 'exec_command', host='a', cwd=str(source), command=['pwd'])
    async def observed_run(command, cwd=None, timeout=None, host=None, max_bytes=16384,
                           trace=False, compute_review=None, stdin=None):
        return dict(outcome)
    monkeypatch.setattr('remote_workbench.tools.commands.ssh_run', observed_run)
    reader = AsyncMock(side_effect=AssertionError('Must not read an unconfirmed outcome'))
    monkeypatch.setattr('remote_workbench.tools.files.read_selected', reader)
    result = await call(server, 'a', 'apply_patch', patch=PATCH, command=['true'], read_files=['value.txt'])
    assert result['success']
    assert all(result['command_result'][key] == value for key, value in outcome.items())
    assert result['files_skipped'] == 'command_not_successful'
    reader.assert_not_awaited()


@pytest.mark.parametrize('outcome', [
    {'success': True, 'execution_state': 'unknown'},
    {'success': True, 'timed_out': True},
    {'success': True, 'executed': False},
])
async def test_explicit_patch_uncertainty_skips_selected_reads(project, monkeypatch, outcome):
    _, server, source, _ = project
    await call(server, 'a', 'exec_command', host='a', cwd=str(source), command=['pwd'])
    monkeypatch.setattr('remote_workbench.tools.patch.edit', AsyncMock(return_value=outcome))
    reader = AsyncMock(side_effect=AssertionError('Unknown patch must not continue'))
    monkeypatch.setattr('remote_workbench.tools.files.read_selected', reader)
    result = await call(server, 'a', 'apply_patch', patch=PATCH, edit_files=['value.txt'])
    assert result['files_skipped'] == 'apply_not_successful'
    reader.assert_not_awaited()


async def test_patch_conflict_skips_command_and_reads(project, monkeypatch):
    _, server, source, _ = project
    await call(server, 'a', 'exec_command', host='a', cwd=str(source), command=['pwd'])
    (source / 'value.txt').write_text('external\n')
    reader = AsyncMock(side_effect=AssertionError('Conflict must not continue'))
    monkeypatch.setattr('remote_workbench.tools.files.read_selected', reader)
    result = await call(server, 'a', 'apply_patch', patch=PATCH, command=['true'], edit_files=['value.txt'])
    assert result['error'] and result['files_skipped'] == 'apply_not_successful'
    assert result['command_skipped'] == 'apply_not_successful'
    assert (source / 'value.txt').read_text() == 'external\n'
    reader.assert_not_awaited()


async def test_followup_read_failure_does_not_turn_applied_patch_into_retryable_error(project, monkeypatch):
    _, server, source, _ = project
    await call(server, 'a', 'exec_command', host='a', cwd=str(source), command=['pwd'])
    monkeypatch.setattr('remote_workbench.tools.files.read_selected', AsyncMock(side_effect=OSError('Unavailable')))
    result = await call(server, 'a', 'apply_patch', patch=PATCH,
        command=[sys.executable, '-c', 'from pathlib import Path;Path("ran").write_text("once")'],
        read_files=['value.txt'])
    assert result['success'] and result['command_result']['exit_code'] == 0
    assert result['files'][0]['error'] == 'read_failed' and 'error' not in result
    assert (source / 'value.txt').read_text() == 'changed\n' and (source / 'ran').read_text() == 'once'


@pytest.mark.parametrize('selection', [
    {'read_files': ['x'], 'edit_files': ['x']}, {'edit_files': ['~/guess']},
    {'read_files': ['x'], 'max_bytes': 0},
])
async def test_bad_post_patch_selection_fails_before_any_transport(project, monkeypatch, selection):
    _, server, source, _ = project
    transport = count_transport(monkeypatch)
    with pytest.raises(ToolError):
        await call(server, 'a', 'apply_patch', patch=PATCH, **selection)
    assert transport == [] and (source / 'value.txt').read_text() == 'original\n'


async def test_large_command_budget_does_not_make_selected_read_fail(project):
    _, server, source, _ = project
    result = await call(server, 'a', 'exec_command', host='a', cwd=str(source),
        command=['pwd'], max_bytes=1048576, read_files=['value.txt'])
    assert result['exit_code'] == 0 and result['files'][0]['content'] == '1\toriginal'


async def test_patch_selection_is_exposed_on_primary_and_advanced_schemas(project):
    _, server, _, _ = project
    tools = {tool.name: tool for tool in await server.list_tools()}
    assert len(tools) == 8
    assert {'read_files', 'edit_files'} <= tools['apply_patch'].inputSchema['properties'].keys()
    described = await call(server, 'a', 'workspace_tools', tool='describe', arguments={'name': 'apply_patch'})
    assert {'read_files', 'edit_files'} <= described['inputSchema']['properties'].keys()
