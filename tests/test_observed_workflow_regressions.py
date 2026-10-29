"""Replay observed workflow failures with local workers, without a model run."""
import copy
import json
import sys

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from remote_workbench import audit
from remote_workbench.tool_interface import INSTRUCTIONS, present_result
from tests.test_execution_latency import count_transport
from tests.test_session_server import call, project


def completed(**changes):
    value = {'exit_code': 0, 'execution_state': 'finished', 'stdout': 'unchanged output\n',
             'remote_task': {'state': 'finished', 'reason': 'finished',
                'origin_nonce': None, 'launch_cwd': '/selected/project/run',
                'actual_cwd': '/selected/project/run', 'cleanup_state': 'process_group_signalled',
                'cleanup_scope': 'process_group', 'tree_confirmed': False, 'hard_process_limit': False}}
    value.update(changes)
    return value


def test_success_projection_keeps_actual_location_limits_and_unmodified_original():
    source = completed()
    original = copy.deepcopy(source)
    reply = present_result('exec_command', source)
    task = reply['remote_task']
    assert task['actual_cwd'] == '/selected/project/run'
    assert not task['tree_confirmed'] and not task['hard_process_limit']
    assert task['cleanup_state'] == 'process_group_signalled'
    assert reply['stdout'] == source['stdout'] and reply['exit_code'] == 0
    assert len(json.dumps(reply)) < len(json.dumps(source))
    assert source == original
    assert present_result('audit_get', source) == original


@pytest.mark.parametrize('change', [
    {'exit_code': 1}, {'execution_state': 'unknown'}, {'timed_out': True},
    {'error': 'connection_lost'},
    {'remote_task': {**completed()['remote_task'], 'cleanup_state': 'unconfirmed'}},
])
def test_unsuccessful_or_uncertain_task_retains_all_origin_details(change):
    source = completed(**change)
    assert present_result('exec_command', source)['remote_task'] == source['remote_task']


def test_different_locations_and_real_identity_are_not_redundant():
    source = completed()
    source['remote_task'].update(launch_cwd='/chosen/link', origin_nonce='original-job-identity',
                                 future_detail={'diagnostic': 'keep'})
    reply = present_result('exec_command', source)['remote_task']
    assert reply['launch_cwd'] == '/chosen/link'
    assert reply['actual_cwd'] == '/selected/project/run'
    assert reply['origin_nonce'] == 'original-job-identity'
    assert reply['future_detail'] == {'diagnostic': 'keep'}


async def test_old_alias_patch_and_output_failures_replay_without_extra_handshakes(project, monkeypatch):
    router, server, source, _ = project
    (source / 'logic.py').write_text('def answer():\n    return 1\n')
    (source / 'test_logic.py').write_text('import unittest\nfrom logic import answer\n'
        'class Tests(unittest.TestCase):\n    def test_answer(self):\n        self.assertEqual(answer(), 1)\n')
    transport = count_transport(monkeypatch)
    # Observed argv/workdir aliases and optional missing project instructions.
    inspected = await call(server, 'a', 'exec_command', host='a', workdir=str(source),
        argv=[sys.executable, '-B', '-m', 'unittest', '-v'],
        read_files=['logic.py', 'test_logic.py', 'AGENTS.md'])
    assert len(transport) == 3  # directory validation, command, selected file read
    assert inspected['exit_code'] == 0 and 'Ran 1 test' in inspected['stderr']
    assert inspected['files'][2]['error']  # Missing optional file never replays the command.
    assert not (source / '.git').exists()
    # No local staging file, full-file hash handshake or line-number arithmetic.
    patch = ['*** Begin Patch', '*** Update File: logic.py', '@@',
             ' def answer():', '-    return 1', '+    return 2',
             '*** Update File: test_logic.py', '@@',
             '-        self.assertEqual(answer(), 1)', '+        self.assertEqual(answer(), 2)',
             '*** End Patch']
    changed = await call(server, 'a', 'apply_patch', patch=patch,
        command=[sys.executable, '-B', '-m', 'unittest', '-v'])
    assert changed['success'] and changed['command_result']['exit_code'] == 0
    assert 'Ran 1 test' in changed['command_result']['stderr']
    state = router.registry.get_or_create('a')
    with audit.scoped_root(router.audit_dir(state.workspace_id)):
        checked = changed['command_result']
        assert (audit.directory(checked['output_id']) / 'stderr').read_text() == checked['stderr']
        diff = (audit.directory(changed['output_id']) / 'diff').read_text()
        assert '-    return 1' in diff and '+    return 2' in diff
        assert 'test_logic.py' in diff
    assert router.resources.active() == []


async def test_observed_missing_delimiter_is_rejected_before_transport_or_write(project, monkeypatch):
    router, server, source, _ = project
    await call(server, 'a', 'exec_command', host='a', cwd=str(source), command=['pwd'])
    transport = count_transport(monkeypatch)
    with pytest.raises(ToolError, match='must end with'):
        await call(server, 'a', 'apply_patch', patch=[
            '*** Begin Patch', '*** Update File: value.txt', '@@', '-original', '+changed'])
    assert transport == [] and (source / 'value.txt').read_text() == 'original\n'
    assert router.resources.active() == []


async def test_shared_instructions_and_catalog_keep_required_boundaries(project):
    _, server, _, _ = project
    tools = {tool.name: tool for tool in await server.list_tools()}
    assert len(tools) == 8
    assert len(INSTRUCTIONS) <= 700
    for boundary in ('New chats start local', 'manual SSH', 'identity/SSH failure',
                     'user confirmation', 'compute_review', 'no remote Codex CLI',
                     'Remote Workbench 操作记录'):
        assert boundary in INSTRUCTIONS
    assert 'activate environments per call' in tools['exec_command'].description
    assert 'Jobs keep their original target' in tools['workspace_switch'].description
