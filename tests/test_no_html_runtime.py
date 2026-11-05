"""Current MCP entrypoints need neither HTTP listeners nor a viewer URL."""
import json
import socket
import sys

import pytest
from mcp.server.fastmcp import FastMCP

from remote_workbench import audit, session_server
from remote_workbench.records_cli import main as records_main


@pytest.mark.parametrize('identity_only', [False, True])
def test_session_startup_never_binds_http_even_with_old_launcher_flag(tmp_path, monkeypatch, identity_only):
    config=tmp_path/'hosts.yaml'
    config.write_text('hosts:\n  test:\n    ssh_host: never-connect.invalid\n')
    args=['remote-workbench-session','--config',str(config),'--data-dir',str(tmp_path/'state'),'--viewer-port','12345']
    if identity_only:args.append('--identity-probe')
    monkeypatch.setattr(sys,'argv',args)
    def forbidden(*args,**kwargs):
        raise AssertionError('HTTP listener must not be opened')
    monkeypatch.setattr(socket.socket,'bind',forbidden)
    seen=[]
    monkeypatch.setattr(FastMCP,'run',lambda self:seen.append(self.name))
    session_server.main()
    assert seen==['remote-workbench-identity-probe' if identity_only else 'remote-workbench-session']


def test_local_records_cli_reads_existing_history_and_byte_pages(tmp_path,capsys):
    workspace='a'*32
    directory=tmp_path/'audit'/workspace
    with audit.scoped_root(directory):
        record_id=audit.create(kind='command',status='finished',exit_code=0,command='printf hello')
        with audit.open_stream(record_id,'stdout') as f:f.write(b'hello\n')
    base=['--data-dir',str(tmp_path),'--workspace-id',workspace]
    records_main(base)
    assert json.loads(capsys.readouterr().out)['records'][0]['id']==record_id
    records_main(base+['--id',record_id])
    assert json.loads(capsys.readouterr().out)['command']=='printf hello'
    records_main(base+['--id',record_id,'--stream','stdout','--max-bytes','4'])
    page=json.loads(capsys.readouterr().out)
    assert page['content']=='hell' and page['next_cursor']==4 and page['truncated']


def test_records_cli_missing_or_invalid_workspace_creates_nothing(tmp_path):
    for workspace in ['../other','b'*32]:
        with pytest.raises(SystemExit) as exc:
            records_main(['--data-dir',str(tmp_path),'--workspace-id',workspace])
        assert exc.value.code==2
    assert not list(tmp_path.iterdir())
