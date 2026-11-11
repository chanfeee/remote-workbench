import asyncio
from pathlib import Path
import shutil
import socket
import sys
import tempfile
from unittest.mock import AsyncMock, MagicMock

import pytest

from remote_workbench.config import HostConfig
from remote_workbench.ssh import SSHExecutor


@pytest.mark.parametrize("probe", [False, True])
async def test_mux_fd_failure_returns_stop_guidance_without_retry(tmp_path, monkeypatch, probe):
    monkeypatch.setenv("REMOTE_WORKBENCH_AUDIT_DIR", str(tmp_path / "audit"))
    create = asyncio.create_subprocess_exec
    launches = []

    async def failed_transport(*args, **kwargs):
        launches.append(args)
        code = "import sys; sys.stderr.write('mux_client_request_session: send fds failed\\n'); sys.exit(255)"
        return await create(sys.executable, "-c", code, **kwargs)

    monkeypatch.setattr("remote_workbench.ssh.asyncio.create_subprocess_exec", failed_transport)
    executor = SSHExecutor(HostConfig(ssh_host="never-connect.invalid"))
    executor._last_check_ok = True
    if probe:
        with pytest.raises(ValueError, match="Stop remote operations") as error:
            await executor.probe_directory("/tmp")
        diagnostic = str(error.value)
    else:
        result = await executor.run("echo must-not-run", cwd="/tmp")
        assert result.exit_code == 255 and result.execution_state == "remote_state_unknown"
        diagnostic = result.stderr
    assert "Stop remote operations" in diagnostic
    assert "mux_client_request_session: send fds failed" in diagnostic
    assert len(launches) == 1 and not executor._last_check_ok


@pytest.fixture
def control_path():
    # OpenSSH/macOS sun_path is much shorter than pytest's default tmp path.
    with tempfile.TemporaryDirectory(prefix="rwb-mux-", dir="/tmp") as directory:
        yield Path(directory) / "master"


@pytest.mark.parametrize("stale_socket", [False, True])
async def test_disappeared_master_cannot_fall_back_to_a_new_tcp_connection(tmp_path, monkeypatch, stale_socket, control_path):
    monkeypatch.setenv("REMOTE_WORKBENCH_AUDIT_DIR", str(tmp_path / "audit"))
    accepted = []
    async def connection(reader, writer):
        accepted.append(True)
        writer.close()
        await writer.wait_closed()
    listener = await asyncio.start_server(connection, "127.0.0.1", 0)
    port = listener.sockets[0].getsockname()[1]
    control = control_path
    if stale_socket:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as stale:
            stale.bind(str(control))  # Existing, non-listening socket.
    empty_config = tmp_path / "ssh_config"
    empty_config.write_text("")
    real_create = asyncio.create_subprocess_exec
    ssh = shutil.which("ssh")
    assert ssh
    async def isolated_transport(*args, **kwargs):
        assert args[0] == "ssh"
        # Keep production mux/fallback arguments, but isolate the test from
        # personal SSH configuration. This still executes the real OpenSSH.
        return await real_create(ssh, "-F", str(empty_config), *args[1:], **kwargs)
    monkeypatch.setattr("remote_workbench.ssh.asyncio.create_subprocess_exec", isolated_transport)
    executor = SSHExecutor(HostConfig(ssh_host="127.0.0.1", execution_enabled=True, port=port, control_path=str(control), default_cwd="/tmp"))
    executor._last_check_ok = True
    executor._last_check = asyncio.get_running_loop().time()
    try:
        result = await executor.run("echo must-not-run", timeout=3)
        assert result.exit_code == 255 and not result.timed_out
        assert result.execution_state == "remote_state_unknown"
        assert not accepted, "A stale/missing master must never open a new TCP connection"
        assert executor._last_check_ok is False
    finally:
        listener.close()
        await listener.wait_closed()


async def test_connection_check_cancellation_reaps_only_its_client(monkeypatch):
    entered = asyncio.Event()
    async def communicate():
        entered.set()
        await asyncio.Future()
    process = MagicMock(returncode=None)
    process.communicate = communicate
    process.wait = AsyncMock(return_value=-9)
    monkeypatch.setattr("remote_workbench.ssh.asyncio.create_subprocess_exec", AsyncMock(return_value=process))
    executor = SSHExecutor(HostConfig(ssh_host="never-connect.invalid"))
    task = asyncio.create_task(executor.check_connection())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    process.kill.assert_called_once_with()
    process.wait.assert_awaited_once_with()
