"""Inline activity and diff cards backed by the chat-scoped audit spool."""
import json
from pathlib import Path
from uuid import uuid4

from mcp.server.fastmcp import Context
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import CallToolResult, TextContent, ToolAnnotations

from remote_workbench import audit
from remote_workbench.call_arguments import command_text
from remote_workbench.presentation import parse_unified_diff, summarize_record
from remote_workbench.session_context import get_codex_identity
from remote_workbench.tools.audit import ssh_audit_get

RESOURCE_URI = 'ui://remote-workbench/operation-v11.html'
BATCH_RESOURCE_URI = 'ui://remote-workbench/batch-v1.html'
HISTORICAL_RESOURCE_URIS = tuple(f'ui://remote-workbench/operation-v{version}.html'
                                 for version in (2, 4, 5, 6, 7, 8, 9, 10))
CARD_TOOLS = frozenset({'review_activity', 'apply_patch'})
PREVIEW_BYTES = 4096


def _record(record_id):
    if not record_id:
        return {}
    path = audit.directory(record_id) / 'record.json'
    if path.stat().st_size > 512 * 1024:
        return {}
    return json.loads(path.read_text())


def _step(result, tool, command, *, operation=None, source=None, preview_bytes=PREVIEW_BYTES):
    operation_id, output_id = result.get('operation_id'), result.get('output_id')
    if operation is None:
        operation = _record(operation_id)
    if source is None:
        source = operation if output_id == operation_id else _record(output_id)
    view = summarize_record(operation, source)
    streams = {}
    if output_id:
        directory = audit.directory(output_id)
        for name in ('diff',) if tool == 'apply_patch' else ('stdout', 'stderr'):
            if (directory / name).is_file():
                streams[name] = audit.read_output(output_id, name, max_bytes=preview_bytes)
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


def attach_patch_card(blocks, arguments):
    """Decorate a patch result without changing its text or retrying work."""
    result = json.loads(blocks[0].text)
    command = arguments.get('command', arguments.get('cmd'))
    steps = [_step(result, 'apply_patch', command)]
    if isinstance(result.get('command_result'), dict):
        steps.append(_step(result['command_result'], 'exec_command', command))
    card_id = steps[0]['operation_id'] or steps[0]['output_id'] or uuid4().hex
    return CallToolResult(content=list(blocks), _meta={
        'openai/widgetSessionId': f'remote-workbench:{card_id}',
        'remote-workbench/operation': {'steps': steps},
    })


def install(server, router):
    """Bind selected command reviews and patch diffs to inline cards."""
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

    @server.tool(title='远程命令汇总', structured_output=False,
                 annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False))
    async def review_activity(operation_ids: list[str], ctx: Context = None) -> CallToolResult:
        """Show one compact card for selected exec_command operation_ids in order, including a single command. Use once per group of up to 8 commands or before the final reply. Reads this chat's saved results locally."""
        if not 1 <= len(operation_ids) <= 8 or len(set(operation_ids)) != len(operation_ids):
            raise ToolError('Pass 1 to 8 distinct command operation IDs.')
        steps = []
        preview_bytes = PREVIEW_BYTES // len(operation_ids)
        try:
            identity = get_codex_identity(ctx)
            state = router.registry.get_or_create(identity.thread_id)
            with audit.scoped_root(router.audit_dir(state.workspace_id)):
                for operation_id in operation_ids:
                    operation = await ssh_audit_get(operation_id)
                    if operation.get('kind') != 'tool' or operation.get('tool') not in {'ssh_run', 'exec_command', 'exec_batch'}:
                        raise ToolError('Select command operation IDs returned by exec_command.')
                    if operation.get('status') == 'running':
                        raise ToolError('Review activity after the commands finish.')
                    output_id = operation.get('output_id')
                    source = await ssh_audit_get(output_id) if output_id else {}
                    if source and (source.get('kind') != 'command' or source.get('parent_id') not in (None, operation_id)):
                        raise ToolError('The saved output does not belong to the selected command.')
                    command = (operation.get('arguments') or {}).get('command')
                    facts = dict(operation_id=operation_id, output_id=output_id,
                                 cwd=source.get('actual_cwd') or source.get('cwd'),
                                 execution_state=source.get('execution_state'), error=operation.get('error'),
                                 steps=(source.get('remote_task') or {}).get('batch'))
                    step = _step(facts, 'exec_command', command, operation=operation, source=source,
                                 preview_bytes=preview_bytes)
                    if not step.get('batch_steps') and isinstance(step['command'], str) and len(step['command']) > 1024:
                        step['command'] = step['command'][:1024] + '…'
                    steps.append(step)
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            raise ToolError('A selected command record is unavailable in this chat.') from None
        if len({step['host'] for step in steps}) > 1:
            raise ToolError('Select commands from the same host for each activity card.')
        count = sum(len(step.get('batch_steps', ())) or 1 for step in steps)
        if count > 32:
            raise ToolError('Review at most 32 command steps per card. Split large groups.')
        summary = json.dumps({'success': True, 'commands': count, 'operation_ids': operation_ids},
                             separators=(',', ':'))
        return CallToolResult(content=[TextContent(type='text', text=summary)], _meta={
            'openai/widgetSessionId': f'remote-workbench:activity:{uuid4().hex}',
            'remote-workbench/operation': {'steps': steps, 'compact': True},
        })

    for name in CARD_TOOLS:
        server._tool_manager.get_tool(name).meta = {
            'ui': {'resourceUri': RESOURCE_URI}, 'openai/outputTemplate': RESOURCE_URI,
        }
    server._tool_manager.get_tool('read_output').meta = {'ui': {'visibility': ['model', 'app']}}

    @server._mcp_server.call_tool(validate_input=False)
    async def call_with_card(name, arguments):
        blocks = await server.call_tool(name, arguments)
        if name != 'apply_patch':
            return blocks
        try:
            identity = get_codex_identity(server.get_context())
            state = router.registry.get_or_create(identity.thread_id)
            with audit.scoped_root(router.audit_dir(state.workspace_id)):
                return attach_patch_card(blocks, arguments or {})
        except (OSError, ValueError, TypeError, KeyError, IndexError, AttributeError):
            # Completed work must stay a valid result even if its preview is unavailable.
            return blocks
