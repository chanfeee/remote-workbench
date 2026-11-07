"""New operations stay native text; archived cards remain readable on demand."""
import json
import shutil
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

from mcp import ClientSession, StdioServerParameters, types
from mcp.client.stdio import stdio_client
from mcp.server.lowlevel.server import request_ctx
from mcp.shared.context import RequestContext
import pytest

from remote_workbench import audit, inline_app
from tests.test_session_server import call, project


async def wire_call(server, thread, name, **arguments):
    meta = types.RequestParams.Meta(threadId=thread, sessionId='text-test')
    token = request_ctx.set(RequestContext(request_id=1, meta=meta, session=None, lifespan_context=None))
    try:
        request = types.CallToolRequest(params=types.CallToolRequestParams(name=name, arguments=arguments))
        return (await server._mcp_server.request_handlers[types.CallToolRequest](request)).root
    finally:
        request_ctx.reset(token)


def text_result(wire):
    assert not wire.isError
    assert wire.structuredContent is None
    assert len(wire.content) == 1 and wire.content[0].type == 'text'
    return json.loads(wire.content[0].text)


async def test_native_text_preserves_command_facts_and_chat_scoped_output(project):
    _, server, source, _ = project
    text = text_result(await wire_call(server, 'a', 'exec_command', host='a', cwd=str(source),
                                      command=['printf', '<script>untrusted</script>\n']))
    assert text['cwd'] == str(source) and text['exit_code'] == 0
    assert text['stdout'] == '<script>untrusted</script>\n'
    page = text_result(await wire_call(server, 'a', 'read_output', output_id=text['output_id'], max_bytes=256))
    assert page['content'] == text['stdout']
    other = await wire_call(server, 'b', 'read_output', output_id=text['output_id'], max_bytes=256)
    assert other.isError


async def test_patch_text_keeps_original_diff_and_followup_result(project):
    _, server, source, _ = project
    await call(server, 'a', 'workspace_switch', host='a', cwd=str(source))
    wire = await wire_call(server, 'a', 'apply_patch',
        patch='*** Begin Patch\n*** Update File: value.txt\n@@\n-original\n+updated\n*** End Patch',
        command=['printf', 'checked'])
    text = text_result(wire)
    assert (source / 'value.txt').read_text() == 'updated\n'
    assert text['command_result']['stdout'] == 'checked'
    assert text['command_result']['exit_code'] == 0
    diff = text_result(await wire_call(server, 'a', 'read_output', output_id=text['output_id'], stream='diff'))
    assert '-original\n+updated' in diff['content']
    patch, check = wire.meta['remote-workbench/operation']['steps']
    assert patch['tool'] == 'apply_patch' and '-original\n+updated' in patch['streams']['diff']['content']
    assert check['tool'] == 'exec_command' and check['streams']['stdout']['content'] == 'checked'


async def test_native_batch_retains_all_steps_in_one_tool_result(project):
    _, server, source, _ = project
    labels = [f'step {number}' for number in range(1, 9)]
    result = text_result(await wire_call(server, 'a', 'exec_command', host='a', cwd=str(source),
        command={'commands': [['printf', label + '\n'] for label in labels]}))
    assert result['exit_code'] == 0 and result['batch_state'] == 'finished'
    assert [step['exit_code'] for step in result['steps']] == [0] * 8
    assert [step['state'] for step in result['steps']] == ['finished'] * 8
    assert result['stdout'] == ''.join(f'[command {number}/8]\n{label}\n'
                                      for number, label in enumerate(labels, 1))
    saved = text_result(await wire_call(server, 'a', 'read_output', output_id=result['output_id']))
    assert saved['content'] == result['stdout']


async def test_native_history_retains_independent_results_without_reexecution(project, monkeypatch):
    router, server, source, _ = project
    results = [await wire_call(server, 'a', 'exec_command', host='a', cwd=str(source),
                               command=['printf', label]) for label in ('first', 'second')]
    texts = [text_result(result) for result in results]
    assert len({value['output_id'] for value in texts}) == 2
    executor = router.snapshot(router.registry.get_or_create('a')).workspace.executor
    async def forbidden(*args, **kwargs):
        raise AssertionError('reading saved results must not execute commands')
    monkeypatch.setattr(executor, 'run', forbidden)
    for wire, expected in zip(results, ('first', 'second')):
        restored = types.CallToolResult.model_validate_json(wire.model_dump_json(by_alias=True))
        value = text_result(restored)
        assert value['stdout'] == expected
        page = text_result(await wire_call(server, 'a', 'read_output', output_id=value['output_id']))
        assert page['content'] == expected


async def test_batch_card_returns_one_result_with_independent_history(project):
    _, server, source, _ = project
    command = {'commands': [['printf', '%s\n', f'step {index}'] for index in range(1, 9)]}
    card_ids = []
    for tool in ('exec_command', 'exec_batch'):
        wire = await wire_call(server, 'a', tool, host='a', cwd=str(source), command=command)
        assert not wire.isError and wire.structuredContent is None
        assert len(wire.content) == 1 and wire.content[0].type == 'text'
        result = json.loads(wire.content[0].text)
        assert result['batch_state'] == 'finished'
        assert [step['exit_code'] for step in result['steps']] == [0] * 8
        card_id = f"remote-workbench:{result['operation_id']}"
        assert wire.meta['openai/widgetSessionId'] == card_id
        card, = wire.meta['remote-workbench/operation']['steps']
        assert all(f'step {index}' in card['command'] for index in range(1, 9))
        assert card['batch_steps'] == [{'state': 'finished', 'exit_code': 0}] * 8
        assert card['streams']['stdout']['content'] == result['stdout']
        card_ids.append(card_id)
    assert len(set(card_ids)) == 2


async def test_process_failure_remains_visible_in_text(project):
    _, server, source, _ = project
    text = text_result(await wire_call(server, 'a', 'exec_command', host='a', cwd=str(source),
        command=['python3', '-c', "import sys;print('problem',file=sys.stderr);sys.exit(7)"]))
    assert text['exit_code'] == 7 and text['stderr'] == 'problem\n'
    assert text['execution_state'] == 'finished'


@pytest.mark.parametrize('tool,state,timed_out', [
    ('exec_command', 'remote_state_unknown', False),
    ('exec_command', 'remote_state_unknown', True),
    ('exec_command', 'cancellation_requested', False),
    ('apply_patch', 'remote_state_unknown', False),
    ('apply_patch', 'remote_state_unknown', True),
])
async def test_unknown_zero_exit_survives_native_text(project, monkeypatch, tool, state, timed_out):
    router, server, source, _ = project
    await call(server, 'a', 'workspace_switch', host='a', cwd=str(source))
    executor = router.snapshot(router.registry.get_or_create('a')).workspace.executor
    original = executor.run
    dispatches = []
    async def lose_completion(*args, **kwargs):
        dispatches.append(args)
        result = await original(*args, **kwargs)
        task = dict(result.remote_task or {}, cleanup_state='unconfirmed')
        audit.update(result.output_id, execution_state=state, remote_task=task,
                     status='timeout' if timed_out else 'finished', timed_out=timed_out)
        return replace(result, execution_state=state, remote_task=task, timed_out=timed_out)
    monkeypatch.setattr(executor, 'run', lose_completion)
    if tool == 'exec_command':
        wire = await wire_call(server, 'a', tool, command=['printf', 'observed'], read_files=['value.txt'])
    else:
        wire = await wire_call(server, 'a', tool,
            patch='*** Begin Patch\n*** Update File: value.txt\n@@\n-original\n+observed\n*** End Patch',
            command=['printf', 'MUST_NOT_RUN'], read_files=['value.txt'])
        assert (source / 'value.txt').read_text() == 'observed\n'
    result = text_result(wire)
    assert result['execution_state'] == state
    with audit.scoped_root(router.audit_dir(router.registry.get_or_create('a').workspace_id)):
        record = json.loads((audit.directory(result['output_id']) / 'record.json').read_text())
    assert record['exit_code'] == 0 and record['execution_state'] == state
    assert record['remote_task']['cleanup_state'] == 'unconfirmed'
    if tool == 'exec_command':
        assert result['exit_code'] == 0 and result['remote_task']['cleanup_state'] == 'unconfirmed'
    assert bool(result.get('timed_out')) is timed_out
    assert 'files' not in result and 'command_result' not in result
    assert result.get('files_skipped') and len(dispatches) == 1


async def test_review_refusal_is_plain_text_without_execution(project, monkeypatch):
    router, server, source, _ = project
    await call(server, 'a', 'workspace_switch', host='a', cwd=str(source))
    executor = router.snapshot(router.registry.get_or_create('a')).workspace.executor
    async def forbidden(*args, **kwargs):
        raise AssertionError('review refusal must not dispatch SSH')
    monkeypatch.setattr(executor, 'run', forbidden)
    result = text_result(await wire_call(server, 'a', 'exec_command', command='make -j64 --help'))
    assert result['executed'] is False and result['error'] == 'compute_review_required'


async def test_card_preview_is_bounded_and_respects_the_text_budget(project):
    _, server, source, _ = project
    wire = await wire_call(server, 'a', 'exec_command', host='a', cwd=str(source),
        command=['python3', '-c', "print('x'*9000)"], max_bytes=256)
    result = text_result(wire)
    assert len(result['stdout'].encode()) <= 256 and result['truncated']
    preview = wire.meta['remote-workbench/operation']['steps'][0]['streams']['stdout']
    assert len(preview['content'].encode()) <= inline_app.PREVIEW_BYTES and preview['truncated']
    full = text_result(await wire_call(server, 'a', 'read_output', output_id=result['output_id'], max_bytes=16384))
    assert len(full['content']) == 9001


async def test_stdio_restores_command_and_diff_cards_and_keeps_archived_resources(tmp_path):
    config = tmp_path / 'hosts.yaml'
    config.write_text('hosts:\n  test:\n    ssh_host: never-connect.invalid\n')
    params = StdioServerParameters(command=sys.executable, args=['-m','remote_workbench.session_server',
        '--config',str(config),'--data-dir',str(tmp_path/'state'),'--viewer-port','0'])
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as client:
            await client.initialize()
            tools = (await client.list_tools()).tools
            assert len(tools) == 8
            for tool in tools:
                meta = tool.meta or {}
                expected = inline_app.RESOURCE_URI if tool.name in {'exec_command', 'apply_patch'} else None
                assert meta.get('ui', {}).get('resourceUri') == expected
                assert meta.get('openai/outputTemplate') == expected
            batch = (await client.read_resource(inline_app.BATCH_RESOURCE_URI)).contents[0]
            assert batch.mimeType == 'text/html;profile=mcp-app'
            assert batch.meta['openai/widgetShowCodexWidgetInline'] is True
            content = (await client.read_resource(inline_app.RESOURCE_URI)).contents[0]
            assert content.mimeType == 'text/html;profile=mcp-app'
            assert content.meta['openai/widgetShowCodexWidgetInline'] is True
            assert content.meta['ui']['csp']['connectDomains'] == []
            for uri in inline_app.HISTORICAL_RESOURCE_URIS:
                historical = (await client.read_resource(uri)).contents[0]
                assert historical.text == content.text
                assert historical.meta['openai/widgetShowCodexWidgetInline'] is False
            result = await client.call_tool('exec_command', {'command':'must never run'}, meta={'threadId':'local'})
            assert result.isError and 'no remote workspace' in result.content[0].text


def test_batch_card_bridge_renders_the_text_result_on_demand():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node.js is needed for the batch card bridge harness')
    subprocess.run([node, str(Path(__file__).with_name('batch_card.cjs'))], check=True, timeout=20)


def test_archived_card_bridge_paging_and_untrusted_text():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node.js is needed for the archived UI bridge harness')
    subprocess.run([node, str(Path(__file__).with_name('inline_app.cjs'))], check=True, timeout=20)


def test_many_archived_cards_bound_background_and_repeated_render_work():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node.js is needed for the archived UI scheduling harness')
    subprocess.run([node, str(Path(__file__).with_name('inline_app_performance.cjs'))], check=True, timeout=20)
