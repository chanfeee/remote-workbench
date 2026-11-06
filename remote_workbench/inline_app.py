"""Read-only MCP Apps cards backed by the existing, chat-scoped audit spool."""
from __future__ import annotations

import json
from pathlib import Path

from mcp.types import CallToolResult

from remote_workbench import audit
from remote_workbench.presentation import parse_unified_diff, summarize_record
from remote_workbench.session_context import get_codex_identity

RESOURCE_URI = 'ui://remote-workbench/operation-v5.html'
CARD_TOOLS = frozenset({'exec_command', 'apply_patch'})
PREVIEW_BYTES = 4096


def _record(record_id):
    if not record_id:
        return {}
    path = audit.directory(record_id) / 'record.json'
    # Do not load arbitrarily large historical arguments just to draw a card.
    if path.stat().st_size > 512 * 1024:
        return {}
    return json.loads(path.read_text())


def _step(result, tool):
    operation = _record(result.get('operation_id'))
    source = _record(result.get('output_id'))
    view = summarize_record(operation, source)
    streams = {}
    for name in (('diff',) if tool == 'apply_patch' else ('stdout', 'stderr')):
        if result.get('output_id') and (audit.directory(result['output_id']) / name).is_file():
            streams[name] = audit.read_output(result['output_id'], name, max_bytes=PREVIEW_BYTES)
    diff = streams.get('diff')
    diff_summary = (parse_unified_diff(diff['content'], complete=not diff['truncated']) if diff else None)
    remote_task = result.get('remote_task') or source.get('remote_task') or {}
    return dict(tool=tool, output_id=result.get('output_id'), operation_id=result.get('operation_id'),
                host=view.get('host'), cwd=view.get('workspace_path'), command=view.get('command'),
                state=result.get('execution_state') or view.get('state'),
                exit_code=result.get('exit_code', source.get('exit_code')),
                elapsed_ms=source.get('elapsed_ms', operation.get('elapsed_ms')),
                check_only=bool(result.get('check_only')), changed=result.get('changed'),
                executed=result.get('executed'),
                timed_out=bool(result.get('timed_out') or source.get('timed_out') or source.get('status') == 'timeout'),
                cleanup_state=remote_task.get('cleanup_state'), error=result.get('error'),
                diff_summary=diff_summary, streams=streams)


def attach_card(blocks, tool):
    """Leave model-visible content byte-identical; UI previews live only in _meta.

    Failure to decorate a completed command must never turn it into a tool error
    (which might cause a caller to retry a write).
    """
    try:
        result = json.loads(blocks[0].text)
        steps = [_step(result, tool)]
        if isinstance(result.get('command_result'), dict):
            steps.append(_step(result['command_result'], 'exec_command'))
        return CallToolResult(content=list(blocks), _meta={'remote-workbench/operation': {'steps': steps}})
    except (OSError, ValueError, TypeError, KeyError, IndexError, AttributeError):
        return blocks


def install(server, router):
    """Attach a static resource and wire-only decorator; no extra model tool."""
    @server.resource(RESOURCE_URI, mime_type='text/html;profile=mcp-app',
                     meta={'ui': {'prefersBorder': True,
                                  'csp': {'connectDomains': [], 'resourceDomains': []}},
                           'openai/ui': {'availableDisplayModes': ['inline']}})
    def operation_card() -> str:
        return Path(__file__).with_name('operation_card.html').read_text()

    for name in CARD_TOOLS:
        server._tool_manager.get_tool(name).meta = {'ui': {'resourceUri': RESOURCE_URI},
                                                   'openai/outputTemplate': RESOURCE_URI}
    # The card invokes only this existing, local, read-only spool reader.
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
                return attach_card(blocks, name)
        except (OSError, ValueError, TypeError):
            return blocks
