"""Verify structured data reaches the original supervisor/patch protections."""
import json
import sys

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from remote_workbench.call_arguments import command_text, patch_text
from remote_workbench.session_server import create_server
from tests.test_session_server import call, project, router_at


async def test_argv_arguments_are_literal_and_audited(project):
    router, server, source, _ = project
    await call(server, "a", "workspace_switch", host="a", cwd=str(source))
    literals = ['space name', '"quotes"', "single'quote", r'back\slash', '`ticks`',
                '${HOME}', '$(touch unwanted-marker)', '; touch unwanted-marker', '中文']
    args = [sys.executable, '-c', 'import json,sys;print(json.dumps(sys.argv[1:],ensure_ascii=False))', *literals]
    result = await call(server, "a", "exec_command", command=args)
    assert result['exit_code'] == 0 and json.loads(result['stdout']) == literals
    assert not (source / 'unwanted-marker').exists()
    record = await call(server, "a", "audit_get", operation_id=result['output_id'])
    assert record['command'] == command_text(args)
    assert router.resources.active() == []


async def test_patch_line_array_keeps_check_and_apply_protections(project):
    router, server, source, _ = project
    await call(server, "a", "workspace_switch", host="a", cwd=str(source))
    await call(server, "a", "exec_command", command=['git', 'init', '-q'])
    lines = ['*** Begin Patch', '*** Update File: value.txt', '@@', '-original',
             '+literal "quotes" `ticks` ${value} \\ 中文', '*** End Patch']
    checked = await call(server, "a", "workspace_tools", tool="apply_patch", arguments={"patch":lines,"check_only":True})
    assert checked['success'] and (source / 'value.txt').read_text() == 'original\n'
    changed = await call(server, "a", "workspace_tools", tool="apply_patch", arguments={"patch":lines})
    assert changed['success']
    assert (source / 'value.txt').read_text() == lines[4][1:] + '\n'
    assert router.resources.active() == []


@pytest.mark.parametrize('arguments', [
    {'command': []}, {'command': ['', 'x']}, {'command': ['echo', '\x00']},
    {'command': 'pwd', 'max_bytes': 1}, {'command': 'pwd', 'timeout': 0},
    {'command': 'pwd', 'cwd': '~/project'},
])
async def test_invalid_command_fails_before_execution_or_claim(project, monkeypatch, arguments):
    router, server, source, _ = project
    bound = await call(server, "a", "workspace_switch", host="a", cwd=str(source))
    executor = router.snapshot(router.registry.get(bound['workspace_id'])).workspace.executor
    async def forbidden(*args, **kwargs):
        pytest.fail('invalid arguments must not reach SSH')
    monkeypatch.setattr(executor, 'run', forbidden)
    with pytest.raises(ToolError):
        await call(server, "a", "exec_command", **arguments)
    assert router.resources.active() == []


@pytest.mark.parametrize('patch', [[], ['*** Begin Patch\n*** End Patch'],
    r'*** Begin Patch\n*** End Patch', '*** Begin Patch\n*** Update File: value.txt\n'])
async def test_invalid_patch_fails_before_execution_or_claim(project, monkeypatch, patch):
    router, server, source, _ = project
    bound = await call(server, "a", "workspace_switch", host="a", cwd=str(source))
    executor = router.snapshot(router.registry.get(bound['workspace_id'])).workspace.executor
    async def forbidden(*args, **kwargs):
        pytest.fail('invalid patch must not reach SSH')
    monkeypatch.setattr(executor, 'run', forbidden)
    with pytest.raises(ToolError):
        await call(server, "a", "workspace_tools", tool="apply_patch", arguments={"patch":patch})
    assert router.resources.active() == []


async def test_argv_retains_authentication_and_compute_review_guards(project):
    router, server, source, _ = project
    await call(server, "a", "workspace_switch", host="a", cwd=str(source))
    refusal = await call(server, "a", "exec_command", command=['sshpass', '-p', 'secret-marker', 'ssh', 'host'])
    assert refusal['executed'] is False and refusal['error'] == 'authentication_requires_user_terminal'
    assert 'secret-marker' not in json.dumps(refusal)
    reviewed = await call(server, "a", "exec_command", command=['make', '-j64'])
    assert reviewed['executed'] is False
    assert router.resources.active() == []


async def test_gateway_rejects_misspelled_fields_instead_of_ignoring(tmp_path):
    server = create_server(router_at(tmp_path))
    with pytest.raises(ToolError, match='Unknown argument names: timeot'):
        await call(server, 'a', 'workspace_tools', tool='exec_command',
                   arguments={'command': ['pwd'], 'timeot': 5})


async def test_switch_initial_inspection_accepts_argv(project):
    _, server, source, _ = project
    result = await call(server, 'a', 'workspace_switch', host='a', cwd=str(source), command=['pwd'])
    assert result['initial']['exit_code'] == 0
    assert result['initial']['stdout'].strip() == str(source.resolve())


async def test_observed_parameter_aliases_work_without_retry(project):
    _, server, source, _ = project
    switched = await call(server, 'a', 'workspace_switch', host='a', workdir=str(source), argv=['pwd'])
    assert switched['initial']['stdout'].strip() == str(source)
    for arguments in ({'argv':['pwd'], 'workdir':'.'}, {'cmd':['pwd']}):
        value = await call(server, 'a', 'exec_command', **arguments)
        assert value['exit_code'] == 0 and value['stdout'].strip() == str(source)
    value = await call(server, 'a', 'workspace_tools', tool='exec_command', arguments={'argv':['pwd']})
    assert value['exit_code'] == 0


@pytest.mark.parametrize('arguments', [
    {'argv':['pwd'], 'command':'pwd'}, {'argv':['pwd'], 'cmd':'pwd'},
    {'command':'pwd', 'workdir':'.', 'cwd':'.'}, {'argv':'pwd'},
    {'command':'pwd', 'timeot':1}, {'command':['echo', {'large': 'private-value'*1000}]},
    {'command': {'commands': [['pwd']], 'stop_on_error':'false'}},
])
async def test_conflicting_or_invalid_arguments_fail_before_any_remote_call(tmp_path, arguments):
    router = router_at(tmp_path)
    with pytest.raises(ToolError) as failure:
        await call(create_server(router), 'a', 'exec_command', **arguments)
    assert not router._executors
    assert len(str(failure.value)) < 1000 and 'private-value' not in str(failure.value)
