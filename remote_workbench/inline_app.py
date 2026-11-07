"""One inline text card per batch and resources for older saved cards."""
from pathlib import Path
from uuid import uuid4

from mcp.types import CallToolResult, TextContent

RESOURCE_URI = 'ui://remote-workbench/operation-v8.html'
BATCH_RESOURCE_URI = 'ui://remote-workbench/batch-v1.html'
HISTORICAL_RESOURCE_URIS = tuple(f'ui://remote-workbench/operation-v{version}.html'
                                 for version in (2, 4, 5, 6, 7))


def batch_card_result(text, result):
    card_id = result.get('operation_id') or result.get('output_id') or uuid4().hex
    return CallToolResult(content=[TextContent(type='text', text=text)],
                          _meta={'openai/widgetSessionId': f'remote-workbench:batch:{card_id}'})


def install(server):
    """Bind the batch entry point and keep previously saved resources readable."""
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

    server._tool_manager.get_tool('exec_batch').meta = {
        'ui': {'resourceUri': BATCH_RESOURCE_URI}, 'openai/outputTemplate': BATCH_RESOURCE_URI,
    }
    for uri in (RESOURCE_URI, *HISTORICAL_RESOURCE_URIS):
        server.resource(uri, mime_type='text/html;profile=mcp-app', meta={
            'ui': {'prefersBorder': True,
                   'csp': {'connectDomains': [], 'resourceDomains': []}},
            'openai/ui': {'availableDisplayModes': ['inline']},
            'openai/widgetShowCodexWidgetInline': False,
            'openai/widgetMinFrameHeight': 0,
            'openai/widgetHeightHint': 96,
        })(operation_card)

    # Previously saved cards can still page their chat-scoped output. Visibility
    # alone does not link this reader to an HTML template.
    server._tool_manager.get_tool('read_output').meta = {'ui': {'visibility': ['model', 'app']}}
