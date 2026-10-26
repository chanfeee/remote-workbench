"""Exercise the actual JavaScript boundary; no SSH or remote commands."""
import json
from pathlib import Path
import shutil
import subprocess

import pytest

from remote_workbench.tool_interface import CALL_SOURCE_INSTRUCTIONS
from remote_workbench.session_server import create_server
from tests.test_session_server import router_at

NODE = shutil.which("node")
ROOT = Path(__file__).resolve().parents[1]


def run_source(source, response=None, capture_texts=False):
    if NODE is None:
        pytest.skip("Node.js is only needed to execute the client JavaScript examples")
    harness = r'''
const fs = require("node:fs");
const input = JSON.parse(fs.readFileSync(0, "utf8"));
const source = input.source;
const AsyncFunction = Object.getPrototypeOf(async function() {}).constructor;
let calls = [];
let returned;
let texts = [];
const tools = new Proxy({}, {get: () => async args => {
    calls.push(args);
    return input.response ?? {success: true};
}});
(async () => {
    try {
        await new AsyncFunction("tools", "text", source)(tools, v => { returned = v; texts.push(v); });
        process.stdout.write(JSON.stringify({calls, returned, ...(input.capture_texts ? {texts} : {})}));
    } catch (error) {
        process.stdout.write(JSON.stringify({calls, error: error.name}));
    }
})();
'''
    result = subprocess.run([NODE, "-e", harness], input=json.dumps(dict(
        source=source, response=response, capture_texts=capture_texts)),
                            text=True, capture_output=True, timeout=10, check=True)
    return json.loads(result.stdout)


def test_original_overescaped_delimiter_fails_before_mcp_submission():
    source = 'await tools.mcp__remote_workbench__apply_patch({cwd: \\"/remote/project\\", patch: "x"});'
    assert run_source(source) == {"calls": [], "error": "SyntaxError"}


def test_documented_patch_example_submits_once_with_exact_newlines():
    result = run_source((ROOT / "docs/examples/mcp-apply-patch.js").read_text())
    assert result == {"calls": [{"cwd": "/remote/project", "edits": [
        {"edit_id": "a" * 32, "start_line": 1, "end_line": 1, "new": ['print("new")']}]}],
        "returned": {"success": True}}


def test_line_array_preserves_code_literals_without_interpolation():
    lines = ["*** Begin Patch", "*** Update File: example.py", "@@", '-print("old")',
             '+print("quotes \\\\ path `ticks` ${literal} 中文")', "*** End Patch", ""]
    cwd = '/remote/project with "quotes"/\\path'
    source = ('const args = {cwd:' + json.dumps(cwd) + ',patch:[' +
              ','.join(json.dumps(line) for line in lines) + ']};' +
              'text(await tools.mcp__remote_workbench__apply_patch(args));')
    assert run_source(source)["calls"] == [{"cwd": cwd, "patch": lines}]


def test_session_publishes_call_boundary_guidance(tmp_path):
    server = create_server(router_at(tmp_path))
    assert server._mcp_server.instructions.count(CALL_SOURCE_INSTRUCTIONS) == 1


def test_legacy_workspace_publishes_same_call_boundary_guidance():
    from remote_workbench.config import HostConfig
    from remote_workbench.ssh import SSHExecutor
    from remote_workbench.workspace import Workspace, create_server as create_workspace_server

    workspace = Workspace("example", "host", "/remote/project", SSHExecutor(HostConfig("host")))
    server = create_workspace_server(workspace)
    assert server._mcp_server.instructions.count(CALL_SOURCE_INSTRUCTIONS) == 1


def test_generated_bridge_preserves_json_and_code_literals():
    from remote_workbench.call_source import render_call
    values = ['"quotes"', "single'quote", "\\path", '`ticks`', '${literal}',
              '\u2028\u2029', "中文", '-\"import argparse\"', '+', '*** End Patch']
    for value in values:
        arguments = {"cwd": "/remote/project", "edits": [
            {"path": "example.py", "old": [value], "new": [value + " changed"]}],
            "metadata": {"__proto__": {"literal": True}}}
        assert run_source(render_call('apply_patch', arguments))["calls"] == [arguments]


def test_bridge_rejects_code_in_tool_name():
    from remote_workbench.call_source import render_call
    with pytest.raises(ValueError):
        render_call('apply_patch);throw Error()', {})


def test_generated_bridge_normalizes_observed_argv_alias_without_touching_values():
    from remote_workbench.call_source import render_call
    values = ['python3', '-c', 'print("\\\\n `ticks` ${literal} 中文")']
    result = run_source(render_call('exec_command', {'argv': values, 'workdir': '/a b'}))
    assert result['calls'] == [{'command': values, 'cwd': '/a b'}]


def test_generated_calls_compose_without_variable_redeclaration():
    from remote_workbench.call_source import render_call
    source = render_call('read_file', {'path':'a'}) + render_call('read_file', {'path':'b'})
    assert run_source(source)['calls'] == [{'path':'a'}, {'path':'b'}]


def test_bridge_prints_text_once_without_mcp_envelope():
    from remote_workbench.call_source import render_call
    source = render_call('read_file', {'path': 'file.py'})
    result = run_source(source, response={'content': [
        {'type':'text', 'text':'first'}, {'type':'text', 'text':'second'}]}, capture_texts=True)
    assert result['texts'] == ['first', 'second']
    error = run_source(source, response={'isError': True, 'content': [
        {'type':'text', 'text':'rejected'}]}, capture_texts=True)
    assert error['texts'] == ['Remote Workbench error', 'rejected']


@pytest.mark.parametrize('source', [
    '{"tool":"read_file","arguments":{},"arguments":{}}',
    '{"tool":"read_file","arguments":{"path":"a","path":"b"}}',
    '{"tool":"read_file","arguments":{"value":NaN}}',
    '{"tool":"read_file","arguments":{},"extra":true}',
])
def test_adapter_rejects_ambiguous_or_non_json_input(source):
    from remote_workbench.call_source import parse_request
    with pytest.raises(ValueError):
        parse_request(source)


def test_adapter_rejects_invalid_utf8_and_conflicting_aliases():
    from remote_workbench.call_source import render_call
    with pytest.raises(ValueError):
        render_call('exec_command', {'command':'true', 'argv':['false']})
    with pytest.raises(UnicodeError):
        render_call('read_file', {'path':'\ud800'})


@pytest.mark.parametrize("source", [
    'const patch = ["@@", import argparse"];',
    'const patch = ["@@", -", "*** End Patch"];',
])
def test_recorded_diff_marker_and_javascript_grammar_mixup(source):
    # Reduced from the native and remote failures in the second benchmark.
    assert run_source(source) == {"calls": [], "error": "SyntaxError"}


def test_generated_example_is_exactly_the_json_request_adapter():
    from remote_workbench.call_source import render_call
    value = json.loads((ROOT / "docs/examples/edit-request.json").read_text())
    source = (ROOT / "docs/examples/mcp-apply-patch.js").read_text()
    assert source.endswith(render_call(value['tool'], value['arguments']))
