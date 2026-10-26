"""Optional deterministic JSON-to-client-source adapter, never an evaluator.

The MCP cannot intercept a client's JavaScript parse failure. Clients which
need a JavaScript bridge can generate its source from data instead of having
the model author the wrapper. No remote call or retry occurs in this module.
"""
import argparse
import json
from pathlib import Path
from remote_workbench.command_spec import normalize_arguments

_TOOLS = frozenset({'workspace_info', 'workspace_switch', 'workspace_use_local',
                    'exec_command', 'apply_patch', 'read_file', 'read_output', 'fetch_image', 'workspace_tools'})


def parse_request(source):
    """Reject ambiguous JSON rather than silently taking the last duplicate key."""
    def pairs(items):
        value = {}
        for key, item in items:
            if key in value:
                raise ValueError('Duplicate JSON key: ' + key)
            value[key] = item
        return value
    def nonfinite(value):
        raise ValueError('Nonfinite JSON number')
    value = json.loads(source, object_pairs_hook=pairs, parse_constant=nonfinite)
    if not isinstance(value, dict) or set(value) != {'tool', 'arguments'}:
        raise ValueError('Request must contain exactly tool and arguments')
    return value


def render_call(tool, arguments):
    if not isinstance(tool, str) or tool not in _TOOLS or not isinstance(arguments, dict):
        raise ValueError('Use a known Remote Workbench tool name and an arguments object')
    arguments = normalize_arguments(tool, arguments)
    # Reject strings that cannot cross a UTF-8 transport; never repair their bytes.
    json.dumps(arguments, ensure_ascii=False, allow_nan=False).encode('utf-8')
    # JSON.parse keeps JSON object semantics even for a key such as __proto__.
    payload = json.dumps(arguments, ensure_ascii=True, allow_nan=False, separators=(',', ':'))
    literal = json.dumps(payload, ensure_ascii=True)
    return ('{const r=await tools.mcp__remote_workbench__' + tool + '(JSON.parse(' + literal + '));\n'
            'if(r.isError) text("Remote Workbench error");\n'
            'if(Array.isArray(r.content)){for(const c of r.content) if(c.type==="text") text(c.text);}'
            'else text(r);}\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('request', type=Path, help='JSON object with exactly tool and arguments')
    args = parser.parse_args()
    try:
        value = parse_request(args.request.read_text(encoding='utf-8'))
        source = render_call(value['tool'], value['arguments'])
    except (OSError, TypeError, ValueError) as exc:
        parser.error(str(exc))
    print(source, end='')


if __name__ == '__main__':
    main()
