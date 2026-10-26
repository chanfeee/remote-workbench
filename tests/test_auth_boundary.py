from unittest.mock import AsyncMock
import json

from mcp import types
from mcp.server.lowlevel.server import request_ctx
from mcp.shared.context import RequestContext
import pytest

from remote_workbench import audit
from remote_workbench.auth_boundary import authentication_command
from remote_workbench.config import HostConfig, ServerConfig
from remote_workbench.session_server import create_server
from remote_workbench.ssh import SSHResult
from remote_workbench.tools import commands
from remote_workbench.workspace_registry import WorkspaceTarget
from remote_workbench.workspace_router import SessionRouter


@pytest.mark.parametrize('command', [
    'sshpass -f /example/pass ssh host',
    'env SSHPASS=secret sshpass -e ssh host',
    '/usr/bin/sshpass -p secret ssh host',
    'true; sshpass -p secret ssh host',
    'sudo sshpass -p secret ssh host',
    'sudo -u user sshpass -p secret ssh host',
    'time sshpass -p secret ssh host',
    'time -p sshpass -p secret ssh host',
    'timeout 15s sudo -u user sshpass -p secret ssh host',
    'env -u OTHER SSHPASS=secret sshpass -e ssh host',
    "env -S 'sshpass -p secret ssh host'",
])
def test_authentication_workflows_are_recognized(command):
    assert authentication_command(command)


@pytest.mark.parametrize('command', [
    'rg sshpass README.md', 'printf password', 'echo sshpass', 'pytest -q',
    'time rg sshpass README.md', "python -c 'print(\"sshpass\")'",
])
def test_normal_development_commands_are_unchanged(command):
    assert not authentication_command(command)


async def test_authentication_arguments_never_enter_operation_log(tmp_path, monkeypatch):
    monkeypatch.setenv('REMOTE_WORKBENCH_AUDIT_DIR', str(tmp_path / 'audit'))
    connection = AsyncMock(side_effect=AssertionError('must reject before connecting'))
    monkeypatch.setattr(commands, 'require_connection', connection)
    value = await commands.ssh_run('sshpass -p dummy-sensitive-marker ssh host')
    assert value['executed'] is False
    assert value['error'] == 'authentication_requires_user_terminal'
    text = (audit.directory(value['operation_id']) / 'record.json').read_text()
    assert 'dummy-sensitive-marker' not in text
    connection.assert_not_awaited()


class FakeExecutor:
    """A local-only session executor that records attempts but never connects."""

    def __init__(self, config, **kwargs):
        self.config = config
        self.calls = []

    async def check_connection(self, *args, **kwargs):
        self.calls.append(("check_connection", args, kwargs))
        return True, "fake connection"

    async def run(self, command, **kwargs):
        self.calls.append(("run", command, kwargs))
        return SSHResult("", "fake transport", 255)

    async def run_program(self, argv, **kwargs):
        self.calls.append(("run_program", argv, kwargs))
        return SSHResult("", "fake transport", 255)


async def test_session_wrapped_authentication_commands_are_rejected_before_executor_or_audit(tmp_path):
    router = SessionRouter(
        ServerConfig(hosts={"test": HostConfig("unused.invalid", execution_enabled=True)}),
        tmp_path / "state", executor_factory=FakeExecutor,
    )
    server = create_server(router)
    state = router.registry.get_or_create("auth-guard-test")
    target = WorkspaceTarget("target-id", state.workspace_id, "test", "/source",
                             "/source", "/source")
    state = router.registry.commit_switch(state.workspace_id, 0, target, remote_state_dir="/runtime/unit-test")
    marker = "session-dummy-sensitive-marker"
    meta = types.RequestParams.Meta(**{"threadId": "auth-guard-test", "sessionId": "test"})

    async def call(name, **arguments):
        token = request_ctx.set(RequestContext(
            request_id=1, meta=meta, session=None, lifespan_context=None,
        ))
        try:
            blocks = await server.call_tool(name, arguments)
            assert len(blocks) == 1
            return json.loads(blocks[0].text)
        finally:
            request_ctx.reset(token)

    results = [
        await call("exec_command", command=f"sudo sshpass -p {marker} ssh user@host"),
        await call("start_job", command=f"timeout 10s sshpass -p {marker} ssh user@host",
                   idempotency_key="auth-guard-job"),
    ]

    executor = next(iter(router._executors.values()))
    assert executor.calls == []
    assert all(result["executed"] is False for result in results)
    assert all(result["error"] == "authentication_requires_user_terminal" for result in results)
    assert all(marker not in json.dumps(result) for result in results)
    records = list(router.audit_dir(state.workspace_id).glob("*/record.json"))
    assert records
    assert all(marker not in path.read_text() for path in records)
