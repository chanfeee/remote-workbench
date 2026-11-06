"""Read-only compatibility resources for cards saved by older versions.

New operations use ordinary MCP text. Nothing here decorates tool results or
binds a page to a tool; retaining an old resource does not create a new widget.
"""
from pathlib import Path

RESOURCE_URI = 'ui://remote-workbench/operation-v8.html'
HISTORICAL_RESOURCE_URIS = tuple(f'ui://remote-workbench/operation-v{version}.html'
                                 for version in (2, 4, 5, 6, 7))


def install(server):
    """Keep saved cards readable on demand without enabling inline rendering."""
    def operation_card() -> str:
        return Path(__file__).with_name('operation_card.html').read_text()

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
