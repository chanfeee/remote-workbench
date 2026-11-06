"""MCP wire cards preserve facts, isolation and the eight-tool interface."""
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
    meta = types.RequestParams.Meta(threadId=thread, sessionId='inline-test')
    token = request_ctx.set(RequestContext(request_id=1, meta=meta, session=None, lifespan_context=None))
    try:
        request = types.CallToolRequest(params=types.CallToolRequestParams(name=name, arguments=arguments))
        return (await server._mcp_server.request_handlers[types.CallToolRequest](request)).root
    finally:
        request_ctx.reset(token)


async def test_card_uses_saved_command_facts_and_chat_scoped_output(project):
    router, server, source, _ = project
    result = await wire_call(server, 'a', 'exec_command', host='a', cwd=str(source),
                             command=['printf', '<script>untrusted</script>\n'])
    assert not result.isError
    text = json.loads(result.content[0].text)
    card = result.meta['remote-workbench/operation']['steps'][0]
    assert card['cwd'] == str(source) and card['exit_code'] == 0
    assert card['output_id'] == text['output_id']
    assert card['streams']['stdout']['content'] == text['stdout'] == '<script>untrusted</script>\n'
    assert 'wrapped_command' not in json.dumps(card)
    # The wire-only UI metadata does not duplicate output in model content.
    assert 'remote-workbench/operation' not in result.content[0].text
    page = await wire_call(server, 'a', 'read_output', output_id=text['output_id'], max_bytes=256)
    assert json.loads(page.content[0].text)['content'] == text['stdout']
    other = await wire_call(server, 'b', 'read_output', output_id=text['output_id'], max_bytes=256)
    assert other.isError


async def test_patch_card_has_original_diff_and_followup_process_result(project):
    _, server, source, _ = project
    await call(server, 'a', 'workspace_switch', host='a', cwd=str(source))
    result = await wire_call(server, 'a', 'apply_patch',
        patch='*** Begin Patch\n*** Update File: value.txt\n@@\n-original\n+updated\n*** End Patch',
        command=['printf', 'checked'])
    assert not result.isError and (source / 'value.txt').read_text() == 'updated\n'
    steps = result.meta['remote-workbench/operation']['steps']
    assert '-original\n+updated' in steps[0]['streams']['diff']['content']
    assert steps[0]['diff_summary']['complete']
    assert steps[0]['diff_summary']['files'] == [{'path':'value.txt','added':1,'deleted':1}]
    assert steps[1]['streams']['stdout']['content'] == 'checked'
    assert steps[1]['exit_code'] == 0


async def test_process_failure_is_not_relabelled_as_success(project):
    _, server, source, _ = project
    result = await wire_call(server, 'a', 'exec_command', host='a', cwd=str(source),
                             command=['python3', '-c', "import sys;print('problem',file=sys.stderr);sys.exit(7)"])
    text = json.loads(result.content[0].text)
    step = result.meta['remote-workbench/operation']['steps'][0]
    assert step['exit_code'] == text['exit_code'] == 7
    assert step['streams']['stderr']['content'] == 'problem\n'
    assert step['state'] != 'success'


@pytest.mark.parametrize('tool,state,timed_out,label', [
    ('exec_command', 'remote_state_unknown', False, '状态未知'),
    ('exec_command', 'remote_state_unknown', True, '已超时 · 状态未知'),
    ('exec_command', 'cancellation_requested', False, '取消待确认'),
    ('apply_patch', 'remote_state_unknown', False, '状态未知'),
    ('apply_patch', 'remote_state_unknown', True, '已超时 · 状态未知'),
])
async def test_unconfirmed_zero_exit_survives_wire_and_renderer(project, monkeypatch, tmp_path, tool, state, timed_out, label):
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
    assert not wire.isError
    result = json.loads(wire.content[0].text)
    step = wire.meta['remote-workbench/operation']['steps'][0]
    assert result['execution_state'] == step['state'] == state
    assert step['exit_code'] == 0 and step['cleanup_state'] == 'unconfirmed'
    assert step['timed_out'] is timed_out
    assert 'files' not in result and 'command_result' not in result
    assert result.get('files_skipped') and len(dispatches) == 1
    with audit.scoped_root(router.audit_dir(router.registry.get_or_create('a').workspace_id)):
        record = json.loads((audit.directory(result['output_id']) / 'record.json').read_text())
        assert record['execution_state'] == state and record['exit_code'] == 0
    node = shutil.which('node')
    if node:
        fixture = tmp_path / 'wire-card.json'
        fixture.write_text(json.dumps({'meta': wire.meta, 'tool': tool, 'label': label, 'exit_code': 0}))
        subprocess.run([node, str(Path(__file__).with_name('inline_app.cjs')), str(fixture)], check=True, timeout=20)


async def test_review_refusal_is_rendered_as_not_executed_without_ssh(project, monkeypatch, tmp_path):
    router, server, source, _ = project
    await call(server, 'a', 'workspace_switch', host='a', cwd=str(source))
    executor = router.snapshot(router.registry.get_or_create('a')).workspace.executor
    async def forbidden(*args, **kwargs):
        raise AssertionError('review refusal must not dispatch SSH')
    monkeypatch.setattr(executor, 'run', forbidden)
    wire = await wire_call(server, 'a', 'exec_command', command='make -j64 --help')
    assert not wire.isError
    result = json.loads(wire.content[0].text)
    assert result['executed'] is False and result['error'] == 'compute_review_required'
    assert wire.meta['remote-workbench/operation']['steps'][0]['executed'] is False
    node = shutil.which('node')
    if node:
        fixture = tmp_path / 'refused-card.json'
        fixture.write_text(json.dumps({'meta': wire.meta, 'tool': 'exec_command', 'label': '未执行', 'exit_code': None}))
        subprocess.run([node, str(Path(__file__).with_name('inline_app.cjs')), str(fixture)], check=True, timeout=20)


async def test_card_budget_and_decoration_failure_never_reexecute(project, monkeypatch):
    router, server, source, _ = project
    result = await wire_call(server, 'a', 'exec_command', host='a', cwd=str(source),
                             command=['python3', '-c', "print('x'*9000)"])
    preview = result.meta['remote-workbench/operation']['steps'][0]['streams']['stdout']
    assert len(preview['content'].encode()) <= inline_app.PREVIEW_BYTES
    assert preview['truncated'] and preview['next_cursor'] == inline_app.PREVIEW_BYTES

    def fail(*args):
        raise OSError('local preview unavailable')
    monkeypatch.setattr(inline_app, '_record', fail)
    result = await wire_call(server, 'a', 'exec_command', command=['python3', '-c',
        "from pathlib import Path;p=Path('count');p.write_text(str(int(p.read_text())+1) if p.exists() else '1');print('done')"])
    assert not result.isError and json.loads(result.content[0].text)['stdout'] == 'done\n'
    assert result.meta is None and (source / 'count').read_text() == '1'


async def test_stdio_resource_catalog_and_missing_binding_are_safe(tmp_path):
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
                if tool.name in inline_app.CARD_TOOLS:
                    assert tool.meta['ui']['resourceUri'] == inline_app.RESOURCE_URI
                    assert tool.meta['openai/outputTemplate'] == inline_app.RESOURCE_URI
                    assert not (tool.annotations and tool.annotations.readOnlyHint)
            resource = await client.read_resource(inline_app.RESOURCE_URI)
            content = resource.contents[0]
            assert content.mimeType == 'text/html;profile=mcp-app'
            assert 'ui/initialize' in content.text
            assert content.meta['ui']['csp']['connectDomains'] == []
            result = await client.call_tool('exec_command', {'command':'must never run'}, meta={'threadId':'local'})
            assert result.isError and 'no remote workspace' in result.content[0].text


def test_card_bridge_paging_and_untrusted_text():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node.js is needed for the UI bridge harness')
    subprocess.run([node, str(Path(__file__).with_name('inline_app.cjs'))], check=True, timeout=20)
