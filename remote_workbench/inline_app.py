"""Inline activity and diff cards backed by the chat-scoped audit spool."""
import json
from pathlib import Path
from uuid import uuid4

from mcp.types import CallToolResult

from remote_workbench import audit
from remote_workbench.call_arguments import command_text
from remote_workbench.presentation import parse_unified_diff, summarize_record
from remote_workbench.session_context import get_codex_identity

RESOURCE_URI = 'ui://remote-workbench/operation-v9.html'
BATCH_RESOURCE_URI = 'ui://remote-workbench/batch-v1.html'
HISTORICAL_RESOURCE_URIS = tuple(f'ui://remote-workbench/operation-v{version}.html'
                                 for version in (2, 4, 5, 6, 7, 8))
CARD_TOOLS = frozenset({'exec_command', 'exec_batch', 'apply_patch'})
PREVIEW_BYTES = 4096


def _record(record_id):
    if not record_id:
        return {}
    path = audit.directory(record_id) / 'record.json'
    if path.stat().st_size > 512 * 1024:
        return {}
    return json.loads(path.read_text())


def _step(result, tool, command):
    operation_id, output_id = result.get('operation_id'), result.get('output_id')
    operation = _record(operation_id)
    source = operation if output_id == operation_id else _record(output_id)
    view = summarize_record(operation, source)
    streams = {}
    if output_id:
        directory = audit.directory(output_id)
        for name in ('diff',) if tool == 'apply_patch' else ('stdout', 'stderr'):
            if (directory / name).is_file():
                streams[name] = audit.read_output(output_id, name, max_bytes=PREVIEW_BYTES)
    diff = streams.get('diff')
    diff_summary = parse_unified_diff(diff['content'], complete=not diff['truncated']) if diff else None
    remote_task = result.get('remote_task') or source.get('remote_task') or {}
    step = dict(tool=tool, output_id=output_id, operation_id=operation_id,
                host=result.get('host') or view.get('host'), cwd=result.get('cwd') or view.get('workspace_path'),
                command=view.get('command'), state=result.get('execution_state') or view.get('state'),
                exit_code=result.get('exit_code', source.get('exit_code')),
                elapsed_ms=source.get('elapsed_ms', operation.get('elapsed_ms')),
                check_only=bool(result.get('check_only')), changed=result.get('changed'),
                executed=result.get('executed'), timed_out=bool(result.get('timed_out') or source.get('timed_out')
                    or source.get('status') == 'timeout'), cleanup_state=remote_task.get('cleanup_state'),
                error=result.get('error'), diff_summary=diff_summary, streams=streams)
    if tool == 'exec_command' and isinstance(command, dict):
        outcomes = {entry['index']: entry for entry in result.get('steps') or []}
        lines, batch_steps = [], []
        default_state = 'not_run' if result.get('executed') is False else 'unknown'
        for index, argv in enumerate(command['commands']):
            label = command_text(argv)
            lines.append(f'{index + 1}: {label[:512]}' + ('…' if len(label) > 512 else ''))
            entry = outcomes.get(index, {})
            batch_steps.append({'state': entry.get('state', default_state), 'exit_code': entry.get('exit_code')})
        step.update(command='\n'.join(lines), batch_steps=batch_steps)
    return step


def attach_card(blocks, tool, arguments):
    """Decorate the wire result without changing its text or retrying work."""
    result = json.loads(blocks[0].text)
    command = arguments.get('command', arguments.get('cmd'))
    steps = [_step(result, 'apply_patch' if tool == 'apply_patch' else 'exec_command', command)]
    if isinstance(result.get('command_result'), dict):
        steps.append(_step(result['command_result'], 'exec_command', command))
    card_id = steps[0]['operation_id'] or steps[0]['output_id'] or uuid4().hex
    return CallToolResult(content=list(blocks), _meta={
        'openai/widgetSessionId': f'remote-workbench:{card_id}',
        'remote-workbench/operation': {'steps': steps},
    })


def install(server, router):
    """Bind one rich card per operation and retain resources from saved chats."""
    def operation_card() -> str:
        return Path(__file__).with_name('operation_card.html').read_text()

    @server.resource(BATCH_RESOURCE_URI, mime_type='text/html;profile=mcp-app', meta={
        'ui': {'prefersBorder': True, 'csp': {'connectDomains': [], 'resourceDomains': []}},
        'openai/ui': {'availableDisplayModes': ['inline']},
        'openai/widgetShowCodexWidgetInline': True,
        'openai/widgetMinFrameHeight': 0,
        'openai/widgetHeightHint': 76,
    })
    def batch_card() -> str:
        return Path(__file__).with_name('batch_card.html').read_text()

    for uri in (RESOURCE_URI, *HISTORICAL_RESOURCE_URIS):
        server.resource(uri, mime_type='text/html;profile=mcp-app', meta={
            'ui': {'prefersBorder': True,
                   'csp': {'connectDomains': [], 'resourceDomains': []}},
            'openai/ui': {'availableDisplayModes': ['inline']},
            'openai/widgetShowCodexWidgetInline': uri == RESOURCE_URI,
            'openai/widgetMinFrameHeight': 0,
            'openai/widgetHeightHint': 96,
        })(operation_card)

    for name in CARD_TOOLS:
        server._tool_manager.get_tool(name).meta = {
            'ui': {'resourceUri': RESOURCE_URI}, 'openai/outputTemplate': RESOURCE_URI,
        }
    server._tool_manager.get_tool('read_output').meta = {'ui': {'visibility': ['model', 'app']}}

    @server._mcp_server.call_tool(validate_input=False)
    async def call_with_card(name, arguments):
        blocks = await server.call_tool(name, arguments)
        if name not in CARD_TOOLS:
            return blocks
        try:
            identity = get_codex_identity(server.get_context())
            state = router.registry.get_or_create(identity.thread_id)
            with audit.scoped_root(router.audit_dir(state.workspace_id)):
                return attach_card(blocks, name, arguments or {})
        except (OSError, ValueError, TypeError, KeyError, IndexError, AttributeError):
            # Completed work must stay a valid result even if its preview is unavailable.
            return blocks
