"""Development diagnostics are external to application entry points/releases."""
import asyncio
from contextlib import asynccontextmanager
import json
from pathlib import Path
import stat
import sys

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.server.fastmcp.exceptions import ToolError

from devtools.mcp_timing import Hooks, Recorder, read_summary, summarize, wrapped, wrapped_lease, write_summary
from remote_workbench import audit, session_server
from remote_workbench.config import HostConfig, ServerConfig
from remote_workbench.workspace_router import SessionRouter
from tests.test_session_server import call, project


def events_at(directory):
    return [json.loads(line) for line in (directory / 'events.jsonl').read_text().splitlines()]


def test_private_output_refuses_reuse_and_preserves_existing_data(tmp_path):
    directory = tmp_path / 'timing'
    recorder = Recorder(directory)
    with recorder.span('test.root', root='random-local-correlation'):
        pass
    recorder.close()
    before = (directory / 'events.jsonl').read_bytes()
    with pytest.raises(FileExistsError):
        Recorder(directory)
    assert (directory / 'events.jsonl').read_bytes() == before
    assert stat.S_IMODE(directory.stat().st_mode) & 0o077 == 0
    assert stat.S_IMODE((directory / 'events.jsonl').stat().st_mode) == 0o600
    write_summary(directory)
    assert stat.S_IMODE((directory / 'summary.json').stat().st_mode) == 0o600


def test_results_and_exception_identity_preserved_without_argument_logging(tmp_path):
    recorder = Recorder(tmp_path / 'timing')
    secret = 'PRIVATE-ARGUMENT-CONTENT-EXCEPTION'
    expected = ValueError(secret)

    def success(value):
        return value

    def failure(value):
        raise expected

    value = object()
    with recorder.span('test.root', root='local-id'):
        assert wrapped(success, recorder, 'test.success')(value) is value
        with pytest.raises(ValueError) as caught:
            wrapped(failure, recorder, 'test.failure')(secret)
    assert caught.value is expected
    recorder.close()
    content = (recorder.directory / 'events.jsonl').read_text()
    assert secret not in content
    spans = [row for row in events_at(recorder.directory) if row['kind'] == 'span']
    assert [row['layer'] for row in spans] == ['test.success', 'test.failure', 'test.root']
    assert spans[1]['exception_type'] == 'ValueError'
    assert spans[0]['parent'] == spans[-1]['span']
    assert spans[1]['parent'] == spans[-1]['span']


async def test_cancellation_and_concurrent_request_context_are_preserved(tmp_path):
    recorder = Recorder(tmp_path / 'timing')
    ready = asyncio.Event()

    async def block():
        ready.set()
        await asyncio.Future()

    async def cancelled_request():
        with recorder.span('test.root', root='cancelled'):
            await wrapped(block, recorder, 'test.wait')()

    task = asyncio.create_task(cancelled_request())
    await ready.wait()
    with recorder.span('test.root', root='other'):
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert recorder.trace.get() is None and recorder.parent.get() is None
    assert not recorder.active
    recorder.close()
    spans = [row for row in events_at(recorder.directory) if row['kind'] == 'span']
    wait = next(row for row in spans if row['layer'] == 'test.wait')
    assert wait['trace'] == 'cancelled' and wait['exception_type'] == 'CancelledError'
    other = next(row for row in spans if row['trace'] == 'other')
    assert other['parent'] is None and other['outcome'] == 'returned'


async def test_background_activity_after_request_does_not_keep_profiling(tmp_path):
    recorder = Recorder(tmp_path / 'timing')
    ready, finish = asyncio.Event(), asyncio.Event()

    async def background():
        with recorder.span('test.background'):
            ready.set()
            await finish.wait()
        with recorder.span('test.after'):
            pass

    with recorder.span('test.root', root='local-id'):
        task = asyncio.create_task(background())
        await ready.wait()
    finish.set()
    await task
    recorder.close()
    assert [row['layer'] for row in events_at(recorder.directory) if row['kind'] == 'span'] == ['test.root']


async def test_lease_enter_exit_and_suppression_preserve_semantics(tmp_path):
    recorder = Recorder(tmp_path / 'timing')
    calls = []

    @asynccontextmanager
    async def original():
        calls.append('enter')
        try:
            yield 42
        except ValueError:
            calls.append('suppressed')
        finally:
            calls.append('exit')

    measured = wrapped_lease(original, recorder)
    with recorder.span('test.root', root='local-id'):
        async with measured() as value:
            assert value == 42
            raise ValueError('not logged')
    recorder.close()
    assert calls == ['enter', 'suppressed', 'exit']
    spans = [row for row in events_at(recorder.directory) if row['kind'] == 'span']
    assert {row['layer'] for row in spans} == {'test.root', 'workspace.lease_acquire', 'workspace.lease_release'}


def test_record_io_failure_is_nonfatal_and_does_not_change_result(tmp_path, monkeypatch, capsys):
    recorder = Recorder(tmp_path / 'timing')

    def unavailable(*args):
        raise OSError('private path')

    monkeypatch.setattr('devtools.mcp_timing.os.write', unavailable)
    with recorder.span('test.root', root='local-id'):
        assert wrapped(lambda: 123, recorder, 'test.success')() == 123
    assert recorder.failed
    recorder.close()
    stderr = capsys.readouterr().err
    assert stderr.count('Development timing output unavailable') == 1
    assert 'private path' not in stderr


async def test_explicit_hooks_preserve_catalog_and_replies_then_restore(tmp_path):
    router = SessionRouter(ServerConfig(hosts={'test': HostConfig('never-connect.invalid')}), tmp_path / 'state')
    ordinary = session_server.create_server(router)
    catalog = [tool.model_dump() for tool in await ordinary.list_tools()]
    before = await call(ordinary, 'local', 'workspace_info')
    directory = tmp_path / 'timing'
    assert not directory.exists()
    original_create, original_dispatch = session_server.create_server, session_server.SessionMCP.call_tool
    original_spawn = asyncio.create_subprocess_exec
    recorder = Recorder(directory)
    with Hooks(recorder):
        measured = session_server.create_server(router)
        assert [tool.model_dump() for tool in await measured.list_tools()] == catalog
        assert await call(measured, 'local', 'workspace_info') == before
        with pytest.raises(ToolError, match='threadId is unavailable'):
            await call(measured, None, 'workspace_info')
        assert asyncio.create_subprocess_exec is original_spawn
    assert session_server.create_server is original_create
    assert session_server.SessionMCP.call_tool is original_dispatch
    assert asyncio.create_subprocess_exec is original_spawn
    recorder.close()
    text = (directory / 'events.jsonl').read_text()
    assert 'never-connect.invalid' not in text and str(tmp_path) not in text
    layers = {row.get('layer') for row in events_at(directory)}
    assert {'mcp.dispatch', 'sdk.validate_and_invoke', 'session.handler', 'session.identity', 'reply.projection'} <= layers
    assert not router._executors


def test_hooks_rollback_after_incomplete_install(tmp_path, monkeypatch):
    recorder = Recorder(tmp_path / 'timing')
    original = session_server.SessionMCP.call_tool
    from remote_workbench.ssh import SSHExecutor
    monkeypatch.delattr(SSHExecutor, '_wrap_command')
    with pytest.raises(AttributeError):
        with Hooks(recorder):
            pass
    assert session_server.SessionMCP.call_tool is original
    recorder.close()


async def test_instrumented_command_patch_and_read_keep_normal_supervision_and_records(project, tmp_path):
    router, _, source, _ = project
    recorder = Recorder(tmp_path / 'timing')
    with Hooks(recorder):
        server = session_server.create_server(router)
        first = await call(server, 'a', 'exec_command', host='a', cwd=str(source), command=['printf', 'hello'])
        assert first['stdout'] == 'hello' and first['exit_code'] == 0
        read = await call(server, 'a', 'read_file', path='value.txt')
        assert read['content'] == '1\toriginal'
        changed = await call(server, 'a', 'apply_patch', patch='*** Begin Patch\n*** Update File: value.txt\n@@\n-original\n+edited\n*** End Patch', command=['cat', 'value.txt'])
        assert changed['success'] and changed['command_result']['stdout'] == 'edited\n'
        assert (source / 'value.txt').read_text() == 'edited\n'
        state = router.registry.get_or_create('a')
        with audit.scoped_root(router.audit_dir(state.workspace_id)):
            record = json.loads((audit.directory(first['output_id']) / 'record.json').read_text())
            assert record['command'] == 'printf hello'
            assert record['actual_cwd'] == str(source) and record['remote_task']['cleanup_state'] != 'unconfirmed'
            assert (audit.directory(changed['output_id']) / 'diff').read_text().strip()
        assert not router.resources.active()
    recorder.close()
    events = events_at(recorder.directory)
    spans = [row for row in events if row['kind'] == 'span']
    assert len({row['trace'] for row in spans}) == 3
    layers = {row['layer'] for row in spans}
    assert {'ssh.control_check', 'ssh.prepare', 'ssh.transport', 'workspace.lease_acquire',
            'workspace.lease_release', 'resource.acquire', 'resource.finish', 'operation.execute',
            'reply.projection', 'registry.record_call'} <= layers
    referenced = {row['audit_id'] for row in events if row['kind'] == 'reference'}
    assert {first['output_id'], changed['output_id'], changed['command_result']['output_id']} <= referenced
    assert all(value not in (recorder.directory / 'events.jsonl').read_text()
               for value in ('original', 'edited', 'printf hello', str(source)))


def test_summary_distinguishes_missing_boundaries_and_does_not_add_layers():
    events = [
        {'kind': 'boundary', 'trace': 'a', 'phase': 'decoded_receive', 'mono_ns': 1_000_000},
        {'kind': 'span', 'trace': 'a', 'layer': 'mcp.dispatch', 'start_ns': 2_000_000, 'end_ns': 6_000_000, 'outcome': 'returned'},
        {'kind': 'span', 'trace': 'a', 'layer': 'ssh.transport', 'start_ns': 3_000_000, 'end_ns': 5_000_000, 'outcome': 'returned'},
        {'kind': 'boundary', 'trace': 'a', 'phase': 'reply_queued', 'mono_ns': 7_000_000},
        {'kind': 'boundary', 'trace': 'b', 'phase': 'decoded_receive', 'mono_ns': 8_000_000},
    ]
    a, b = summarize(events)['requests']
    assert a['server_receive_to_reply_queued_ms'] == 6
    assert a['dispatch_ms'] == 4 and a['receive_to_dispatch_ms'] == a['dispatch_to_reply_queued_ms'] == 1
    assert a['layers']['ssh.transport']['inclusive_ms'] == 2
    assert b['complete_dispatch'] is False and b['complete_protocol_boundaries'] is False
    assert 'server_receive_to_reply_queued_ms' not in b


def test_live_summary_marks_partial_tail_without_hiding_complete_corruption(tmp_path):
    recorder = Recorder(tmp_path / 'timing')
    with recorder.span('test.root', root='local-id'):
        pass
    recorder.close()
    path = recorder.directory / 'events.jsonl'
    assert read_summary(recorder.directory)['incomplete_tail_ignored'] is False
    with path.open('ab') as output:
        output.write(b'{"kind":')
    value = read_summary(recorder.directory)
    assert value['incomplete_tail_ignored'] is True and len(value['requests']) == 1
    with path.open('ab') as output:
        output.write(b'BROKEN}\n')
    with pytest.raises(json.JSONDecodeError):
        read_summary(recorder.directory)


async def test_real_development_stdio_correlates_success_and_error_without_reply_changes(tmp_path):
    source = Path(__file__).resolve().parents[1]
    config = tmp_path / 'hosts.yaml'
    config.write_text('hosts:\n  test:\n    ssh_host: never-connect.invalid\n')
    directory = tmp_path / 'timing'
    params = StdioServerParameters(command=sys.executable, args=[
        str(source / 'devtools/mcp_timing.py'), 'serve', '--output', str(directory), '--',
        '--config', str(config), '--data-dir', str(tmp_path / 'state'), '--viewer-port', '0'])
    async with stdio_client(params) as (reader, writer):
        async with ClientSession(reader, writer) as client:
            await client.initialize()
            tools = await client.list_tools()
            assert len(tools.tools) == 8
            good = await client.call_tool('workspace_tools', {'tool': 'workspace_info'}, meta={'threadId': 'private-thread'})
            bad = await client.call_tool('workspace_tools', {'tool': 'workspace_info'})
            assert not good.isError and bad.isError
            value = json.loads(good.content[0].text)
            assert value['mode'] == 'local' and value['host'] is None and value['bound'] is False
            assert 'timing' not in value and 'profile' not in value and good.structuredContent is None
    assert (directory / 'summary.json').exists()
    report = json.loads((directory / 'summary.json').read_text())
    assert len(report['requests']) == 2
    assert all(row['complete_protocol_boundaries'] and row['complete_dispatch'] for row in report['requests'])
    assert {row['outcome'] for row in report['requests']} == {'returned', 'raised'}
    assert all(row['server_receive_to_reply_queued_ms'] >= row['dispatch_ms'] >= 0 for row in report['requests'])
    text = (directory / 'events.jsonl').read_text()
    assert 'private-thread' not in text and 'never-connect.invalid' not in text and str(tmp_path) not in text


def test_production_sources_have_no_development_hook_and_sdist_excludes_it():
    source = Path(__file__).resolve().parents[1]
    assert all('devtools.mcp_timing' not in path.read_text() for path in (source / 'remote_workbench').rglob('*.py'))
    config = (source / 'pyproject.toml').read_text()
    assert '"/devtools"' in config and '"/tests/test_dev_timing.py"' in config
