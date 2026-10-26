import json

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from remote_workbench.call_arguments import patch_text
from remote_workbench.edit_spec import edits_patch
from remote_workbench.session_server import create_server
from tests.test_session_server import call, project, router_at


async def test_primary_edits_schema_has_no_raw_patch_language(tmp_path):
    server = create_server(router_at(tmp_path))
    tools = {tool.name:tool for tool in await server.list_tools()}
    assert len(tools) == 8
    schema = tools['apply_patch'].inputSchema
    assert 'edits' in schema['required'] and 'patch' not in schema['properties']
    assert schema['additionalProperties'] is False
    assert 'PreparedEdit' in schema['$defs']
    assert 'old' not in schema['$defs']['PreparedEdit']['properties']


async def test_legacy_edits_gateway_check_apply_and_atomic_conflict(project):
    router, _, source, _ = project
    (source/'second.txt').write_text('second\n')
    server = create_server(router)
    await call(server, 'a', 'workspace_switch', host='a', cwd=str(source))
    await call(server, 'a', 'exec_command', command=['git','init','-q'])
    edits=[{'path':'value.txt','old':['original'],'new':['import argparse', '+literal', '-literal', '"quotes" `ticks` ${literal}']},
           {'path':'second.txt','old':['second'],'new':['changed']}]
    from remote_workbench.call_source import render_call
    from tests.test_tool_call_syntax import run_source
    generated = run_source(render_call('apply_patch', {'edits':edits,'check_only':True}))
    checked=await call(server, 'a', 'workspace_tools', tool='apply_patch', arguments=generated['calls'][0])
    assert checked['success'] and (source/'value.txt').read_text()=='original\n'
    failed=await call(server, 'a', 'workspace_tools', tool='apply_patch', arguments={'edits':[edits[0],dict(edits[1],old=['wrong'])]})
    assert failed.get('error') and (source/'value.txt').read_text()=='original\n'
    assert (source/'second.txt').read_text()=='second\n'
    changed=await call(server, 'a', 'workspace_tools', tool='apply_patch', arguments={'edits':edits})
    assert changed['success'] and (source/'value.txt').read_text()=='\n'.join(edits[0]['new'])+'\n'
    assert (source/'second.txt').read_text()=='changed\n'
    assert router.resources.active()==[]


@pytest.mark.parametrize('arguments',[
    {'patch':['*** Begin Patch','*** End Patch']},
    {'edits':[{'path':'a','old':['old'],'new':['new']}]},
    {'edits':[{'path':'a','old':['old'],'new':['new'],'extra':'typo'}]},
    {'edits':[{'path':'a','old':['old'],'new':['new']}],'patch':'unwanted'},
])
async def test_primary_invalid_schema_never_contacts_ssh(project, monkeypatch, arguments):
    router, _, _, _=project
    server=create_server(router)
    def forbidden(*args,**kwargs):pytest.fail('schema failure must not construct execution snapshot')
    monkeypatch.setattr(router,'snapshot',forbidden)
    with pytest.raises(ToolError):await call(server,'a','apply_patch',**arguments)
    assert router.resources.active()==[]


async def test_raw_gateway_rejects_recorded_hunk_error_locally_and_keeps_compatibility(project,monkeypatch):
    router, _, source, _=project
    server=create_server(router)
    bound=await call(server,'a','workspace_switch',host='a',cwd=str(source))
    ex=router.snapshot(router.registry.get_or_create('a')).workspace.executor
    original=ex.run
    async def forbidden(*args,**kwargs):pytest.fail('malformed patch must not reach SSH')
    monkeypatch.setattr(ex,'run',forbidden)
    bad=['*** Begin Patch','*** Update File: value.txt','@@','import argparse','*** End Patch']
    with pytest.raises(ToolError,match='Line 4: hunk lines'):
        await call(server,'a','workspace_tools',tool='apply_patch',arguments={'patch':bad})
    assert router.resources.active()==[]
    monkeypatch.setattr(ex,'run',original)
    await call(server,'a','exec_command',command=['git','init','-q'])
    good='*** Begin Patch\n*** Update File: value.txt\n@@\n-original\n+gateway\n*** End Patch\n'
    result=await call(server,'a','workspace_tools',tool='apply_patch',arguments={'patch':good})
    assert result['success'] and (source/'value.txt').read_text()=='gateway\n'


@pytest.mark.parametrize('edits',[
    [], [{'path':'a','old':[],'new':['x']}],
    [{'path':'a\n*** Delete File: b','old':['x'],'new':[]}],
    [{'path':'a','old':['x\ny'],'new':['z']}],
])
def test_malformed_edit_data_is_rejected(edits):
    with pytest.raises(ValueError):edits_patch(edits)
