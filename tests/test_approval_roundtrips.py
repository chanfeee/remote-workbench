"""Accurate approval hints and fewer requests, without relaxing write checks."""
import json
from pathlib import Path
import sys
from unittest.mock import AsyncMock

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.server.fastmcp.exceptions import ToolError

from remote_workbench import audit
from remote_workbench.ssh import SSHResult
from remote_workbench.tool_interface import READ_ONLY_TOOL_NAMES, TOOL_NAMES
from tests.test_execution_latency import count_transport
from tests.test_session_server import call, project, router_at


async def test_only_source_read_operations_advertise_readonly_hints(tmp_path):
    from remote_workbench.session_server import create_server
    router = router_at(tmp_path)
    server = create_server(router)
    tools = await server.list_tools()
    assert {tool.name for tool in tools} == TOOL_NAMES | {'workspace_tools'}
    assert len(tools) == 8
    for tool in tools:
        if tool.name in READ_ONLY_TOOL_NAMES:
            assert tool.annotations.readOnlyHint is True
            assert tool.annotations.destructiveHint is False
            assert tool.annotations.openWorldHint is False
        else:
            assert tool.annotations is None or tool.annotations.readOnlyHint is not True
    assert not router._executors


async def test_inline_command_and_files_are_one_request_with_guarded_origins(project, monkeypatch):
    router, server, source, _ = project
    (source / 'second.py').write_text('SECOND = 1\n')
    launches = count_transport(monkeypatch)
    result = await call(server, 'a', 'exec_command', host='a', cwd=str(source), command=['printf', 'checked'],
                        read_files=['missing.txt'], edit_files=['value.txt', 'second.py'])
    assert len(launches) == 3  # directory check, command, one selected-files worker
    assert result['stdout'] == 'checked' and result['exit_code'] == 0
    missing, first, second = result['files']
    assert missing['error'] and missing['requested_path'] == 'missing.txt'
    assert first['content'] == '1\toriginal' and second['content'] == '1\tSECOND = 1'
    assert first['edit_id'] != second['edit_id']
    state = router.registry.get_or_create('a')
    assert state.revision == 1
    with audit.scoped_root(router.audit_dir(state.workspace_id)):
        for value in result['files']:
            record = json.loads((audit.directory(value['output_id']) / 'record.json').read_text())
            assert record['target_id'] == state.target_id and record['revision'] == 1
            assert record['cwd'] == str(source) and record['parent_id'] == result['operation_id']
            assert record['client_session_id'] == 'test-transport'
    updated = await call(server, 'a', 'apply_patch', edits=[{'edit_id': second['edit_id'], 'new': ['SECOND = 2']}])
    assert updated['success'] and (source / 'second.py').read_text() == 'SECOND = 2\n'
    (source / 'value.txt').write_text('external\n')
    conflict = await call(server, 'a', 'apply_patch', edits=[{'edit_id': first['edit_id'], 'new': ['overwrite']}])
    assert conflict.get('error') and (source / 'value.txt').read_text() == 'external\n'


async def test_selected_paths_follow_actual_command_cwd_without_changing_binding(project):
    router, server, source, _ = project
    run = source / 'run'
    run.mkdir()
    (run / 'value.txt').write_text('RUN\n')
    await call(server, 'a', 'workspace_switch', host='a', cwd=str(source))
    before = router.registry.get_or_create('a')
    result = await call(server, 'a', 'exec_command', cwd='run', command=['pwd'], edit_files=['value.txt'])
    assert result['stdout'].strip() == str(run)
    assert result['files'][0]['content'] == '1\tRUN'
    updated = await call(server, 'a', 'apply_patch', edits=[{'edit_id': result['files'][0]['edit_id'], 'new': ['UPDATED']}])
    assert updated['success'] and (run / 'value.txt').read_text() == 'UPDATED\n'
    assert (source / 'value.txt').read_text() == 'original\n'
    after = router.registry.get_or_create('a')
    assert (after.cwd, after.target_id, after.revision) == (before.cwd, before.target_id, before.revision)


@pytest.mark.parametrize('selection', [
    {'read_files': ['value.txt'], 'edit_files': ['value.txt']},
    {'read_files': ['~/.config']}, {'read_files': ['']}, {'edit_files': ['bad\x00path']},
    {'read_files': [str(i) for i in range(17)]},
])
async def test_bad_selection_fails_before_binding_or_execution(project, monkeypatch, selection):
    router, server, source, _ = project
    launches = count_transport(monkeypatch)
    with pytest.raises(ToolError, match='Select at most 16 distinct file paths'):
        await call(server, 'a', 'exec_command', host='a', cwd=str(source), command=['printf', 'never'], **selection)
    assert not launches
    assert router.registry.get_or_create('a').host is None


async def test_confirmed_nonzero_command_returns_requested_files(project):
    router, server, source, _ = project
    await call(server, 'a', 'workspace_switch', host='a', cwd=str(source))
    result = await call(server, 'a', 'exec_command', command=['python3', '-c', 'raise SystemExit(2)'], read_files=['value.txt'])
    assert result['exit_code'] == 2 and result['files'][0]['content'] == '1\toriginal'
    assert 'files_skipped' not in result
    assert not router.resources.active()


@pytest.mark.parametrize('state,timed_out', [('unknown', False), ('remote_state_unknown', False), ('finished', True)])
async def test_uncertain_or_timed_out_command_skips_reads(project, monkeypatch, state, timed_out):
    router, server, source, _ = project
    await call(server, 'a', 'workspace_switch', host='a', cwd=str(source))
    executor = router.snapshot(router.registry.get_or_create('a')).workspace.executor
    monkeypatch.setattr(executor, 'run', AsyncMock(return_value=SSHResult('', '', 0,
        timed_out=timed_out, execution_state=state)))
    reader = AsyncMock(side_effect=AssertionError('Should not read'))
    monkeypatch.setattr('remote_workbench.tools.files.read_selected', reader)
    result = await call(server, 'a', 'exec_command', command=['printf', 'unconfirmed'], read_files=['value.txt'])
    assert result['files_skipped'] == 'command_not_successful'
    reader.assert_not_awaited()


async def test_followup_read_failure_does_not_replay_or_misreport_completed_write(project, monkeypatch):
    router, server, source, _ = project
    (source / 'counter').write_text('0')
    monkeypatch.setattr('remote_workbench.tools.files.read_selected', AsyncMock(side_effect=OSError('read unavailable')))
    result = await call(server, 'a', 'exec_command', host='a', cwd=str(source), command=['python3', '-c',
        "from pathlib import Path;p=Path('counter');p.write_text(str(int(p.read_text())+1));print('done')"],
        read_files=['value.txt'])
    assert result['exit_code'] == 0 and result['execution_state'] == 'finished' and result['stdout'] == 'done\n'
    assert result['files'][0]['error'] == 'read_failed'
    assert (source / 'counter').read_text() == '1'
    assert not router.resources.active()


async def test_two_request_engineering_flow_keeps_original_outputs_and_managed_diff(project):
    router, server, source, _ = project
    (source / 'calc.py').write_text('def total(values):\n    return sum(values)\n')
    original_tests = ('import unittest\nfrom calc import total\n\nclass Tests(unittest.TestCase):\n'
                      '    def test_numbers(self):\n        self.assertEqual(total([1, 2, 3]), 6)\n'
                      '    def test_empty(self):\n        self.assertEqual(total([]), 0)\n')
    (source / 'test_calc.py').write_text(original_tests)
    inspected = await call(server, 'a', 'exec_command', host='a', cwd=str(source),
        command=['python3', '-m', 'unittest', '-v'], edit_files=['calc.py', 'test_calc.py'])
    assert 'Ran 2 tests' in inspected['stderr'] and inspected['exit_code'] == 0
    first, second = inspected['files']
    changed = await call(server, 'a', 'apply_patch', edits=[
        {'edit_id': first['edit_id'], 'new': ['def total(values):', '    return sum(x for x in values if x is not None)']},
        {'edit_id': second['edit_id'], 'new': (original_tests +
            '    def test_missing(self):\n        self.assertEqual(total([1, None, 2]), 3)\n').splitlines()}],
        command=['python3', '-m', 'unittest', '-v'])
    assert changed['success'] and changed['command_result']['exit_code'] == 0
    assert 'Ran 3 tests' in changed['command_result']['stderr']
    state = router.registry.get_or_create('a')
    with audit.scoped_root(router.audit_dir(state.workspace_id)):
        assert (audit.directory(inspected['output_id']) / 'stderr').read_text() == inspected['stderr']
        assert (audit.directory(changed['output_id']) / 'diff').read_text().strip()
    assert not router.resources.active()


async def test_actual_stdio_exports_hints_and_optional_selection_schema(tmp_path):
    config = tmp_path / 'hosts.yaml'
    config.write_text('hosts:\n  test:\n    ssh_host: never-connect.invalid\n')
    params = StdioServerParameters(command=sys.executable, args=['-m', 'remote_workbench.session_server',
        '--config', str(config), '--data-dir', str(tmp_path / 'state'), '--viewer-port', '0'])
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as client:
            await client.initialize()
            catalog = await client.list_tools()
            assert len(catalog.tools) == 8
            for tool in catalog.tools:
                assert bool(tool.annotations and tool.annotations.readOnlyHint) == (tool.name in READ_ONLY_TOOL_NAMES)
            command = next(tool for tool in catalog.tools if tool.name == 'exec_command')
            assert {'read_files', 'edit_files'} <= command.inputSchema['properties'].keys()
            assert 'read_files' not in command.inputSchema.get('required', [])
            reply = await client.call_tool('workspace_tools', {'tool': 'workspace_info'}, meta={'threadId': 'local-approval-test'})
            value = json.loads(reply.content[0].text)
            assert value['mode'] == 'local' and value['host'] is None
