"""MCP adapters load only when selected, while legacy tools remain callable."""
from pathlib import Path
import subprocess
import sys


def test_session_and_workspace_do_not_initialize_legacy_server(tmp_path):
    source = """
import asyncio
import sys
from pathlib import Path
for optional in ('fastapi', 'websockets', 'asyncssh'):
    sys.modules[optional] = None
from remote_workbench.config import ServerConfig
from remote_workbench.session_server import create_server
from remote_workbench.workspace import Workspace, create_server as fixed_server
from remote_workbench.workspace_router import SessionRouter

async def check():
    router = SessionRouter(ServerConfig(), Path(sys.argv[1]))
    session = create_server(router)
    fixed = fixed_server(Workspace('test', 'never-connect.invalid', '/tmp', None))
    assert len(await session.list_tools()) == 9
    assert len(await fixed.list_tools()) == 20
    assert not router._executors
    assert 'remote_workbench.server' not in sys.modules
    assert 'remote_workbench.audit_web' not in sys.modules
    assert not any(name.startswith('scripts.offline') for name in sys.modules)
    from remote_workbench.server import mcp
    tools = {tool.name for tool in await mcp.list_tools()}
    assert len(tools) == 29
    assert {'ssh_run', 'ssh_read_file', 'ssh_apply_patch', 'ssh_job_wait'} <= tools

asyncio.run(check())
"""
    subprocess.run(
        [sys.executable, "-c", source, str(tmp_path / "state")],
        cwd=Path(__file__).resolve().parents[1], check=True, capture_output=True, text=True,
    )
