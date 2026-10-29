#!/usr/bin/env python3
"""Source-checkout-only MCP timing. Never imported by the application."""
from __future__ import annotations

import argparse
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
import functools
import inspect
import json
import os
from pathlib import Path
import sys
import threading
import time
import uuid


class Recorder:
    """Private local timing events; no arguments, contents or exception messages."""

    def __init__(self, directory):
        directory = Path(directory)
        directory.mkdir(parents=True, mode=0o700, exist_ok=False)
        self.directory = directory
        self.fd = os.open(directory / 'events.jsonl', os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        self.lock = threading.Lock()
        self.trace = ContextVar('dev_timing_trace', default=None)
        self.parent = ContextVar('dev_timing_parent', default=None)
        self.pending = {}
        self.active = set()
        self.failed = False
        self.emit({'kind': 'header', 'schema': 'development-mcp-timing/v1',
                   'wall_ns': time.time_ns(), 'mono_ns': time.perf_counter_ns()})

    def emit(self, event):
        if self.failed or self.fd is None:
            return
        try:
            data = (json.dumps(event, separators=(',', ':')) + '\n').encode()
            with self.lock:
                while data:
                    count = os.write(self.fd, data)
                    if count <= 0:
                        raise OSError('No timing bytes written')
                    data = data[count:]
        except OSError:
            self.failed = True
            # A diagnostic failure cannot turn a successful operation into a retry.
            print('Development timing output unavailable; operation behavior is unchanged.', file=sys.stderr)

    @contextmanager
    def span(self, layer, *, root=None):
        if root is None and self.trace.get() not in self.active:
            yield
            return
        trace_token = self.trace.set(root) if root is not None else None
        if root is not None:
            self.active.add(root)
        old_parent = self.parent.get() if root is None else None
        span = uuid.uuid4().hex
        parent_token = self.parent.set(span)
        started = time.perf_counter_ns()
        outcome = 'returned'
        exception_type = None
        try:
            yield
        except BaseException as exc:
            outcome = 'raised'
            exception_type = type(exc).__name__
            raise
        finally:
            ended = time.perf_counter_ns()
            event = {'kind': 'span', 'trace': self.trace.get(), 'span': span,
                     'parent': old_parent, 'layer': layer, 'start_ns': started,
                     'end_ns': ended, 'outcome': outcome}
            if exception_type:
                event['exception_type'] = exception_type
            self.parent.reset(parent_token)
            if trace_token is not None:
                self.trace.reset(trace_token)
            if event['trace'] in self.active:
                self.emit(event)
            if root is not None:
                self.active.discard(root)

    def received(self, identifier):
        trace = uuid.uuid4().hex
        self.pending[(type(identifier), identifier)] = trace
        self.emit({'kind': 'boundary', 'trace': trace, 'phase': 'decoded_receive',
                   'mono_ns': time.perf_counter_ns()})

    def reply_queued(self, identifier):
        trace = self.pending.pop((type(identifier), identifier), None)
        if trace:
            self.emit({'kind': 'boundary', 'trace': trace, 'phase': 'reply_queued',
                       'mono_ns': time.perf_counter_ns()})

    def request_trace(self, server):
        try:
            identifier = server._mcp_server.request_context.request_id
            trace = self.pending.get((type(identifier), identifier))
        except LookupError:
            trace = None
        return trace or uuid.uuid4().hex

    def reference(self, identity):
        trace = self.trace.get()
        if (trace in self.active and isinstance(identity, str) and len(identity) == 32
                and all(char in '0123456789abcdef' for char in identity)):
            self.emit({'kind': 'reference', 'trace': trace, 'audit_id': identity})

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None


def wrapped(function, recorder, layer, *, dispatch=False):
    if inspect.iscoroutinefunction(function):
        @functools.wraps(function)
        async def async_call(*args, **kwargs):
            root = recorder.request_trace(args[0]) if dispatch else None
            with recorder.span(layer, root=root):
                return await function(*args, **kwargs)
        return async_call

    @functools.wraps(function)
    def sync_call(*args, **kwargs):
        with recorder.span(layer):
            return function(*args, **kwargs)
    return sync_call


def wrapped_lease(function, recorder):
    @functools.wraps(function)
    @asynccontextmanager
    async def lease(*args, **kwargs):
        manager = function(*args, **kwargs)
        with recorder.span('workspace.lease_acquire'):
            value = await manager.__aenter__()
        try:
            yield value
        except BaseException:
            error = sys.exc_info()
            with recorder.span('workspace.lease_release'):
                suppressed = await manager.__aexit__(*error)
            if not suppressed:
                raise
        else:
            with recorder.span('workspace.lease_release'):
                await manager.__aexit__(None, None, None)
    return lease


def rpc_root(message):
    value = getattr(message, 'message', message)
    return getattr(value, 'root', value)


class ReceiveProbe:
    def __init__(self, stream, recorder):
        self.stream, self.recorder = stream, recorder

    def __getattr__(self, name):
        return getattr(self.stream, name)

    async def __aenter__(self):
        await self.stream.__aenter__()
        return self

    async def __aexit__(self, *error):
        return await self.stream.__aexit__(*error)

    def __aiter__(self):
        return self

    async def __anext__(self):
        import anyio
        try:
            return await self.receive()
        except anyio.EndOfStream:
            raise StopAsyncIteration

    async def receive(self):
        message = await self.stream.receive()
        root = rpc_root(message)
        if getattr(root, 'method', None) == 'tools/call':
            self.recorder.received(root.id)
        return message


class SendProbe:
    def __init__(self, stream, recorder):
        self.stream, self.recorder = stream, recorder

    def __getattr__(self, name):
        return getattr(self.stream, name)

    async def __aenter__(self):
        await self.stream.__aenter__()
        return self

    async def __aexit__(self, *error):
        return await self.stream.__aexit__(*error)

    async def send(self, message):
        result = await self.stream.send(message)
        root = rpc_root(message)
        if getattr(root, 'id', None) is not None and getattr(root, 'method', None) is None:
            self.recorder.reply_queued(root.id)
        return result


class Hooks:
    """Instrument fixed local boundaries only; never hook subprocess creation."""

    def __init__(self, recorder):
        self.recorder = recorder
        self.originals = []

    def replace(self, owner, name, value):
        original = getattr(owner, name)
        self.originals.append((owner, name, original))
        setattr(owner, name, value)

    def measure(self, owner, name, layer, **kwargs):
        self.replace(owner, name, wrapped(getattr(owner, name), self.recorder, layer, **kwargs))

    def __enter__(self):
        try:
            self.install()
        except BaseException:
            self.__exit__(None, None, None)
            raise
        return self

    def __exit__(self, *_):
        for owner, name, original in reversed(self.originals):
            setattr(owner, name, original)
        self.originals.clear()

    def install(self):
        from mcp.server.fastmcp import FastMCP
        import mcp.server.fastmcp.server as sdk
        from remote_workbench import audit, session_server
        from remote_workbench.tools import commands
        from remote_workbench.resource_claims import ResourceClaims
        from remote_workbench.ssh import SSHExecutor
        from remote_workbench.workspace_registry import Registry
        from remote_workbench.workspace_router import SessionRouter

        self.measure(session_server.SessionMCP, 'call_tool', 'mcp.dispatch', dispatch=True)
        self.measure(FastMCP, 'call_tool', 'sdk.validate_and_invoke')
        self.measure(session_server, 'get_codex_identity', 'session.identity')
        self.measure(session_server, 'present_result', 'reply.projection')
        self.replace(SessionRouter, 'lease', wrapped_lease(SessionRouter.lease, self.recorder))
        for name in ('switch', 'snapshot', 'executor'):
            self.measure(SessionRouter, name, 'route.' + name)
        for name in ('get_or_create', 'get', 'record_call'):
            self.measure(Registry, name, 'registry.' + name)
        for name in ('acquire', 'finish'):
            self.measure(ResourceClaims, name, 'resource.' + name)
        for name, layer in [('check_connection', 'ssh.control_check'),
                            ('_wrap_command', 'ssh.prepare'), ('run', 'ssh.transport')]:
            self.measure(SSHExecutor, name, layer)
        original_audit_create = audit.create

        @functools.wraps(original_audit_create)
        def audit_create(*args, **kwargs):
            with self.recorder.span('audit.create'):
                identity = original_audit_create(*args, **kwargs)
            self.recorder.reference(identity)
            return identity
        self.replace(audit, 'create', audit_create)
        for name in ('update', 'open_stream', 'read_output'):
            self.measure(audit, name, 'audit.' + name)
        # Session routing binds the functions held in TOOLS, not the legacy
        # server's Tool.fn objects. Follow that actual path, including the
        # explicitly requested command after a patch.
        functions = {function: wrapped(function, self.recorder, 'operation.execute')
                     for _, function, _ in session_server.TOOLS}
        self.replace(session_server, 'TOOLS', [(name, functions[function], description)
                     for name, function, description in session_server.TOOLS])
        self.replace(commands, 'ssh_run', functions[commands.ssh_run])

        original_create = session_server.create_server

        @functools.wraps(original_create)
        def create(*args, **kwargs):
            instance = original_create(*args, **kwargs)
            for tool in instance._tool_manager._tools.values():
                self.measure(tool, 'fn', 'session.handler')
            return instance
        self.replace(session_server, 'create_server', create)

        original_stdio = sdk.stdio_server

        @asynccontextmanager
        async def stdio(*args, **kwargs):
            async with original_stdio(*args, **kwargs) as (read, write):
                yield ReceiveProbe(read, self.recorder), SendProbe(write, self.recorder)
        self.replace(sdk, 'stdio_server', stdio)


def summarize(events):
    """Inclusive spans overlap. Boundary gaps do not measure the App/client."""
    requests = {}
    header = None
    for event in events:
        if event['kind'] == 'header':
            header = event
            continue
        record = requests.setdefault(event['trace'], {'boundaries': {}, 'spans': [], 'audit_ids': []})
        if event['kind'] == 'boundary':
            record['boundaries'][event['phase']] = event['mono_ns']
        elif event['kind'] == 'span':
            record['spans'].append(event)
        elif event['kind'] == 'reference':
            record['audit_ids'].append(event['audit_id'])
    result = []
    for trace, record in requests.items():
        spans = record['spans']
        dispatch = next((s for s in spans if s['layer'] == 'mcp.dispatch'), None)
        boundaries = record['boundaries']
        received, queued = boundaries.get('decoded_receive'), boundaries.get('reply_queued')
        layers = {}
        for span in spans:
            value = layers.setdefault(span['layer'], {'count': 0, 'inclusive_ms': 0})
            value['count'] += 1
            value['inclusive_ms'] += (span['end_ns'] - span['start_ns']) / 1000000
        row = {'trace': trace, 'complete_dispatch': dispatch is not None,
               'complete_protocol_boundaries': received is not None and queued is not None,
               'layers': layers, 'audit_ids': list(dict.fromkeys(record['audit_ids']))}
        if dispatch is not None:
            row['dispatch_ms'] = (dispatch['end_ns'] - dispatch['start_ns']) / 1000000
            row['outcome'] = dispatch['outcome']
        if received is not None and queued is not None:
            row['server_receive_to_reply_queued_ms'] = (queued - received) / 1000000
            if dispatch is not None:
                row['receive_to_dispatch_ms'] = (dispatch['start_ns'] - received) / 1000000
                row['dispatch_to_reply_queued_ms'] = (queued - dispatch['end_ns']) / 1000000
        result.append(row)
    return {'schema': 'development-mcp-timing-summary/v1', 'clock_anchor': header,
            'requests': result, 'limits': [
                'Inclusive spans overlap; never add different layers as a total.',
                'Decoded receive follows stdio parsing; reply queued precedes actual stdout flush.',
                'App scheduling, client transport, remote CPU and remote internal startup are not measured separately.',
                'No model calls, token accounting, project scan or automatic SSH authentication is performed.',
            ]}


def read_summary(directory):
    directory = Path(directory)
    data = (directory / 'events.jsonl').read_bytes()
    lines = data.split(b'\n')
    # A live writer or interrupted write may leave one partial final line.
    # Complete malformed lines still raise rather than hiding corrupt data.
    partial = bool(lines.pop())
    events = [json.loads(line) for line in lines if line]
    value = summarize(events)
    value['incomplete_tail_ignored'] = partial
    return value


def write_summary(directory):
    directory = Path(directory)
    value = read_summary(directory)
    fd = os.open(directory / 'summary.json', os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as output:
        json.dump(value, output, indent=2)
        output.write('\n')
    return value


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_subparsers(dest='action', required=True)
    serve = actions.add_parser('serve', help='Explicitly launch an instrumented development MCP')
    serve.add_argument('--output', type=Path, required=True, help='New private local directory; never reused')
    serve.add_argument('server_args', nargs=argparse.REMAINDER)
    summary = actions.add_parser('summarize', help='Read an existing development timing file offline')
    summary.add_argument('directory', type=Path)
    args = parser.parse_args(argv)
    if args.action == 'summarize':
        print(json.dumps(read_summary(args.directory), indent=2))
        return
    source = Path(__file__).resolve().parent.parent
    if not (source / 'remote_workbench/session_server.py').is_file():
        parser.error('A source checkout is required; this is not a release entry point')
    sys.path.insert(0, str(source))
    recorder = Recorder(args.output)
    try:
        with Hooks(recorder):
            from remote_workbench.session_server import main as serve_main
            forwarded = args.server_args[1:] if args.server_args[:1] == ['--'] else args.server_args
            previous = sys.argv
            try:
                sys.argv = ['remote-workbench-session', *forwarded]
                serve_main()
            finally:
                sys.argv = previous
    finally:
        recorder.close()
        try:
            write_summary(args.output)
        except (OSError, ValueError):
            print('Development timing summary unavailable; raw events remain local.', file=sys.stderr)


if __name__ == '__main__':
    main()
