"""New operations stay native text; archived cards remain readable on demand."""
import hashlib
import json
import shutil
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

from mcp import ClientSession, StdioServerParameters, types
from mcp.client.stdio import stdio_client
from mcp.server.lowlevel.server import request_ctx
from mcp.shared.context import RequestContext
import pytest

from remote_workbench import audit, inline_app
from tests.test_session_server import call, project


async def wire_call(server, thread, name, **arguments):
    meta = types.RequestParams.Meta(threadId=thread, sessionId='text-test')
    token = request_ctx.set(RequestContext(request_id=1, meta=meta, session=None, lifespan_context=None))
    try:
        request = types.CallToolRequest(params=types.CallToolRequestParams(name=name, arguments=arguments))
        return (await server._mcp_server.request_handlers[types.CallToolRequest](request)).root
    finally:
        request_ctx.reset(token)


def text_result(wire):
    assert not wire.isError
    assert wire.structuredContent is None
    assert len(wire.content) == 1 and wire.content[0].type == 'text'
    return json.loads(wire.content[0].text)


async def test_native_text_preserves_command_facts_and_chat_scoped_output(project):
    _, server, source, _ = project
    wire = await wire_call(server, 'a', 'exec_command', host='a', cwd=str(source),
                           command=['printf', '<script>untrusted</script>\n'])
    text = text_result(wire)
    assert wire.meta is None
    assert 'remote_task' not in text
    assert text['cwd'] == str(source) and text['exit_code'] == 0
    assert text['stdout'] == '<script>untrusted</script>\n'
    record = await call(server, 'a', 'workspace_tools', tool='audit_get',
                        arguments={'operation_id': text['output_id']})
    assert record['remote_task']['cleanup_state'] == 'process_group_signalled'
    page = text_result(await wire_call(server, 'a', 'read_output', output_id=text['output_id'], max_bytes=256))
    assert page['content'] == text['stdout']
    other = await wire_call(server, 'b', 'read_output', output_id=text['output_id'], max_bytes=256)
    assert other.isError


async def test_patch_text_keeps_original_diff_and_followup_result(project):
    _, server, source, _ = project
    await call(server, 'a', 'workspace_switch', host='a', cwd=str(source))
    wire = await wire_call(server, 'a', 'apply_patch',
        patch='*** Begin Patch\n*** Update File: value.txt\n@@\n-original\n+updated\n*** End Patch',
        command=['printf', 'checked'])
    text = text_result(wire)
    assert (source / 'value.txt').read_text() == 'updated\n'
    assert text['command_result']['stdout'] == 'checked'
    assert text['command_result']['exit_code'] == 0
    diff = text_result(await wire_call(server, 'a', 'read_output', output_id=text['output_id'], stream='diff'))
    assert '-original\n+updated' in diff['content']
    card = wire.meta['remote-workbench/operation']
    assert card['compact'] is True
    patch, check = card['steps']
    assert patch['tool'] == 'apply_patch' and '-original\n+updated' in patch['streams']['diff']['content']
    assert check['tool'] == 'exec_command' and check['streams']['stdout']['content'] == 'checked'


async def test_large_patch_card_lists_every_changed_file_before_paging(project):
    _, server, source, _ = project
    (source / '删除.txt').write_text('remove this\n')
    line = 'x' * 5000
    wire = await wire_call(server, 'a', 'apply_patch', host='a', cwd=str(source), patch=[
        '*** Begin Patch', '*** Add File: large.txt', '+' + line,
        '*** Update File: value.txt', '@@', '-original', '+updated',
        '*** Add File: empty.txt', '*** Delete File: 删除.txt', '*** End Patch',
    ])
    result = text_result(wire)
    assert result.get('success'), result
    assert result['file_verification'] == {'checked': 4, 'failed': []}
    assert (source / 'large.txt').read_text() == line + '\n'
    assert (source / 'value.txt').read_text() == 'updated\n'
    assert (source / 'empty.txt').is_file() and not (source / '删除.txt').exists()
    step, = wire.meta['remote-workbench/operation']['steps']
    assert step['file_verification'] == result['file_verification']
    preview, summary = step['streams']['diff'], step['diff_summary']
    assert preview['truncated'] and 'value.txt' not in preview['content']
    assert summary['complete'] and (summary['added'], summary['deleted']) == (2, 2)
    assert [(row['path'], row['added'], row['deleted']) for row in summary['files']] == [
        ('large.txt', 1, 0), ('value.txt', 1, 1), ('empty.txt', 0, 0), ('删除.txt', 0, 1),
    ]
    later_start, later_end = summary['files'][1]['range']
    assert later_start > preview['next_cursor']
    later = text_result(await wire_call(server, 'a', 'read_output', output_id=result['output_id'],
                                       stream='diff', cursor=later_start, max_bytes=later_end - later_start))
    assert '-original\n+updated\n' in later['content'] and 'empty.txt' not in later['content']
    assert later['next_cursor'] == later_end
    tail = text_result(await wire_call(server, 'a', 'read_output', output_id=result['output_id'],
                                      stream='diff', cursor=preview['next_cursor']))
    assert '-original\n+updated\n' in tail['content'] and '-remove this\n' in tail['content']
    assert not tail.get('truncated')
    assert hashlib.sha256((preview['content'] + tail['content']).encode()).hexdigest() == result['patch_sha256']


async def test_git_rename_diff_preserves_both_paths(project):
    _, server, source, _ = project
    patch = ('diff --git a/value.txt b/renamed.txt\nsimilarity index 100%\n'
             'rename from value.txt\nrename to renamed.txt\n')
    wire = await wire_call(server, 'a', 'apply_patch', host='a', cwd=str(source), patch=patch)
    result = text_result(wire)
    assert result['success'] and (source / 'renamed.txt').read_text() == 'original\n'
    assert not (source / 'value.txt').exists()
    step, = wire.meta['remote-workbench/operation']['steps']
    row, = step['diff_summary']['files']
    assert (row['previous_path'], row['path']) == ('value.txt', 'renamed.txt')
    saved = text_result(await wire_call(server, 'a', 'read_output', output_id=result['output_id'], stream='diff'))
    assert saved['content'] == patch


def test_partial_patch_card_keeps_all_confirmed_paths():
    applied = [f'/work/path-{index}.txt' for index in range(10)]
    pending = ['/work/pending.txt']
    block = types.TextContent(type='text', text=json.dumps({
        'error': 'partial_apply', 'applied_paths': applied, 'pending_paths': pending,
    }))
    card = inline_app.attach_patch_card([block], {})
    step, = card.meta['remote-workbench/operation']['steps']
    assert step['applied_paths'] == applied and step['pending_paths'] == pending


def test_patch_card_reads_saved_diff_when_optional_record_is_missing(tmp_path):
    patch = '--- a/value.txt\n+++ b/value.txt\n@@ -1 +1 @@\n-old\n+new\n'
    with audit.scoped_root(tmp_path):
        output_id = '1' * 32
        audit.directory(output_id).mkdir()
        with audit.open_stream(output_id, 'diff') as stream:
            stream.write(patch.encode())
        block = types.TextContent(type='text', text=json.dumps({
            'success': True, 'output_id': output_id, 'summary': '1\t1\tvalue.txt\n',
        }))
        card = inline_app.attach_patch_card([block], {})
    step, = card.meta['remote-workbench/operation']['steps']
    assert step['streams']['diff']['content'] == patch
    assert step['diff_summary']['files'][0]['path'] == 'value.txt'


def test_indexes_plain_unified_patch_by_byte_offset():
    first = '--- a/一.txt\n+++ b/一.txt\n@@ -1 +1 @@\n-old\n+new\n'.encode()
    second = b'--- a/two.txt\n+++ /dev/null\n@@ -1 +0,0 @@\n-remove\n'
    files = [{'path': '一.txt'}, {'path': 'two.txt'}]
    inline_app._add_diff_ranges(files, first + second)
    assert files[0]['range'] == [0, len(first)]
    assert files[1]['range'] == [len(first), len(first + second)]


async def test_native_batch_retains_all_steps_in_one_tool_result(project):
    _, server, source, _ = project
    labels = [f'step {number}' for number in range(1, 9)]
    result = text_result(await wire_call(server, 'a', 'exec_command', host='a', cwd=str(source),
        command={'commands': [['printf', label + '\n'] for label in labels]}))
    assert result['exit_code'] == 0 and result['batch_state'] == 'finished'
    assert [step['exit_code'] for step in result['steps']] == [0] * 8
    assert [step['state'] for step in result['steps']] == ['finished'] * 8
    assert result['stdout'] == ''.join(f'[command {number}/8]\n{label}\n'
                                      for number, label in enumerate(labels, 1))
    saved = text_result(await wire_call(server, 'a', 'read_output', output_id=result['output_id']))
    assert saved['content'] == result['stdout']


async def test_native_history_retains_independent_results_without_reexecution(project, monkeypatch):
    router, server, source, _ = project
    results = [await wire_call(server, 'a', 'exec_command', host='a', cwd=str(source),
                               command=['printf', label]) for label in ('first', 'second')]
    texts = [text_result(result) for result in results]
    assert len({value['output_id'] for value in texts}) == 2
    executor = router.snapshot(router.registry.get_or_create('a')).workspace.executor
    async def forbidden(*args, **kwargs):
        raise AssertionError('reading saved results must not execute commands')
    monkeypatch.setattr(executor, 'run', forbidden)
    for wire, expected in zip(results, ('first', 'second')):
        restored = types.CallToolResult.model_validate_json(wire.model_dump_json(by_alias=True))
        value = text_result(restored)
        assert value['stdout'] == expected
        page = text_result(await wire_call(server, 'a', 'read_output', output_id=value['output_id']))
        assert page['content'] == expected


async def test_review_activity_groups_separate_commands_and_a_batch_without_ssh(project, monkeypatch):
    router, server, source, _ = project
    commands = [['printf', 'first'], ['false'],
                {'commands': [['printf', '%s\n', f'step {index}'] for index in range(3, 9)]}]
    operation_ids = []
    for command in commands:
        wire = await wire_call(server, 'a', 'exec_command', host='a', cwd=str(source), command=command)
        assert wire.meta is None
        operation_ids.append(text_result(wire)['operation_id'])
    executor = router.snapshot(router.registry.get_or_create('a')).workspace.executor

    async def forbidden(*args, **kwargs):
        raise AssertionError('Activity review must use only saved records')

    monkeypatch.setattr(executor, 'run', forbidden)
    await call(server, 'a', 'workspace_use_local')
    wire = await wire_call(server, 'a', 'review_activity', operation_ids=operation_ids)
    assert text_result(wire) == {'success': True, 'commands': 8, 'operation_ids': operation_ids}
    card = wire.meta['remote-workbench/operation']
    assert card['compact'] is True
    first, failed, batch = card['steps']
    assert first['streams']['stdout']['content'] == 'first'
    assert failed['exit_code'] == 1
    assert batch['batch_steps'] == [{'state': 'finished', 'exit_code': 0}] * 6
    assert all(f'step {index}' in batch['command'] for index in range(3, 9))
    single = await wire_call(server, 'a', 'review_activity', operation_ids=operation_ids[:1])
    assert text_result(single)['commands'] == 1
    assert single.meta['remote-workbench/operation']['steps'][0]['command'] == first['command']
    assert single.meta['openai/widgetSessionId'] != wire.meta['openai/widgetSessionId']
    other = await wire_call(server, 'b', 'review_activity', operation_ids=operation_ids[:1])
    assert other.isError


async def test_ready_batch_binds_and_keeps_card_available_on_demand(project):
    router, server, source, _ = project
    commands = [['printf', f'check {index}\n'] for index in range(1, 9)]
    wire = await wire_call(server, 'ready', 'exec_batch', host='a', cwd=str(source),
                           command={'commands': commands})
    result = text_result(wire)
    assert result['exit_code'] == 0
    assert result['stdout'].endswith('[command 8/8]\ncheck 8\n')
    state = router.registry.get_or_create('ready')
    assert (state.host, state.cwd, state.revision) == ('a', str(source), 1)
    assert wire.meta is None
    review = await wire_call(server, 'ready', 'review_activity', operation_ids=[result['operation_id']])
    card = review.meta['remote-workbench/operation']
    assert card['compact'] is True
    step, = card['steps']
    assert step['operation_id'] == result['operation_id']
    assert len(step['batch_steps']) == 8
    assert step['streams']['stdout']['content'] == result['stdout']


async def test_read_only_batch_returns_text_and_can_be_reviewed_on_demand(project):
    router, server, source, _ = project
    state = await call(server, 'a', 'workspace_switch', host='a', cwd=str(source))
    # The fixed worker must run even if the interactive shell fails.
    (source.parent / 'native-shell').write_text('#!/bin/sh\nexit 99\n')
    literal = '$(touch forbidden) %n \\n'
    wire = await wire_call(server, 'a', 'inspect_batch',
        command={'commands': [['pwd'], ['printf', '%s\\n', literal], ['printf', '%s\n', 'plain']]})
    result = text_result(wire)
    assert result['exit_code'] == 0
    assert result['stdout'] == (f'[command 1/3]\n{source}\n[command 2/3]\n{literal}\n'
                                '[command 3/3]\nplain\n')
    assert not (source / 'forbidden').exists()
    assert wire.meta is None
    review = await wire_call(server, 'a', 'review_activity', operation_ids=[result['operation_id']])
    card = review.meta['remote-workbench/operation']
    assert card['compact'] is True and len(card['steps'][0]['batch_steps']) == 3
    assert router.registry.get_or_create('a').revision == state['revision']


async def test_read_only_batch_rejects_disallowed_input_before_ssh(project, monkeypatch):
    _, server, source, _ = project
    await call(server, 'a', 'workspace_switch', host='a', cwd=str(source))

    async def no_ssh(*args, **kwargs):
        pytest.fail('Invalid or unbound inspect_batch attempted SSH')

    monkeypatch.setattr('remote_workbench.ssh.asyncio.create_subprocess_exec', no_ssh)
    for argv in (['touch', 'x'], ['/tmp/printf', '%s\\n', 'x'],
                 ['printf', '%b', 'x'], ['pwd', '-L'], ['head', '../x'],
                 ['git', 'diff'], ['git', 'diff', '--cached', '--ext-diff']):
        refused = await wire_call(server, 'a', 'inspect_batch',
            command={'commands': [['pwd'], argv]})
        assert refused.isError and any(message in refused.content[0].text
                                      for message in ('permits only', 'inside the bound directory'))
    unbound = await wire_call(server, 'b', 'inspect_batch', command={'commands': [['pwd']]})
    assert unbound.isError and 'no remote workspace' in unbound.content[0].text
    wrong_target = await wire_call(server, 'a', 'inspect_batch', host='b', command={'commands': [['pwd']]})
    assert wrong_target.isError and 'host and absolute cwd' in wrong_target.content[0].text


async def test_inspect_explicit_target_needs_one_transport_and_keeps_binding(project, monkeypatch):
    router, server, source, other = project
    await call(server, 'a', 'workspace_switch', host='a', cwd=str(source))
    before = router.registry.get_or_create('a')
    transport = router.executor('b', '/').run
    from unittest.mock import AsyncMock
    counted = AsyncMock(wraps=transport)
    monkeypatch.setattr(router.executor('b', '/'), 'run', counted)
    result = text_result(await wire_call(server, 'a', 'inspect_batch', host='b', cwd=str(other),
                                       command={'commands': [['pwd']]}))
    assert result['host'] == 'b' and result['cwd'] == str(other)
    assert result['stdout'].endswith(str(other) + '\n')
    counted.assert_awaited_once()
    assert router.registry.get_or_create('a') == before


async def test_inline_patch_binds_applies_checks_and_reads_in_one_request(project):
    router, server, source, _ = project
    patch = '*** Begin Patch\n*** Update File: value.txt\n@@\n-original\n+updated\n*** End Patch'
    refused = text_result(await wire_call(server, 'new', 'apply_patch', host='a', cwd=str(source),
        patch=patch, command={'commands': [['pwd'], ['make', '-j64']]}))
    assert refused['error'] == 'compute_review_required'
    assert router.registry.get_or_create('new').host is None
    assert (source / 'value.txt').read_text() == 'original\n'
    submit = source / 'sbatch'
    submit.write_text('#!/bin/sh\nprintf "%s\\n" fixture-job-id\n')
    submit.chmod(0o700)
    wire = await wire_call(server, 'new', 'apply_patch', host='a', cwd=str(source),
        patch=patch,
        command={'commands': [['cat', 'value.txt'], [str(submit), '--parsable', 'compare.sbatch', 'diagnose']]},
        read_files=['value.txt'])
    value = text_result(wire)
    assert value['success'] and value['command_result']['exit_code'] == 0
    assert 'updated\n' in value['command_result']['stdout']
    assert 'fixture-job-id\n' in value['command_result']['stdout']
    assert value['files'][0]['content'] == '1\tupdated'
    assert router.registry.get_or_create('new').cwd == str(source)
    assert len(wire.meta['remote-workbench/operation']['steps']) == 2


async def test_read_only_batch_inspects_files_and_git_without_external_drivers(project):
    _, server, source, _ = project

    def git(*args):
        return subprocess.run(['/usr/bin/git', '-C', str(source), *args],
                              check=True, capture_output=True, text=True).stdout.strip()

    git('init', '-q')
    git('config', 'user.email', 'test@example.invalid')
    git('config', 'user.name', 'Read-only fixture')
    git('config', 'commit.gpgsign', 'false')
    (source / '.gitattributes').write_text('value.txt diff=fixture filter=fixture\n')
    git('add', '.gitattributes', 'value.txt')
    git('commit', '-qm', 'initial')
    first = git('rev-parse', 'HEAD')
    (source / 'value.txt').write_text('second\n')
    git('add', 'value.txt')
    git('commit', '-qm', 'second')
    second = git('rev-parse', 'HEAD')
    (source / 'value.txt').write_text('staged\nextra\n')
    git('add', 'value.txt')
    (source / 'value.txt').write_text('unstaged\nextra\n')
    hook = source / 'driver'
    hook.write_text('#!/bin/sh\ntouch driver-ran\n')
    hook.chmod(0o700)
    for key in ('core.fsmonitor', 'diff.external', 'diff.fixture.textconv', 'filter.fixture.clean'):
        git('config', key, str(hook))
    await call(server, 'a', 'workspace_switch', host='a', cwd=str(source))

    wire = await wire_call(server, 'a', 'inspect_batch', command={'commands': [
        ['ls'], ['stat', 'value.txt'], ['head', '-n', '1', 'value.txt'],
        ['wc', '-l', 'value.txt'], ['git', 'diff', '--staged', '--', 'value.txt'],
        ['git', 'diff', first, second], ['git', 'diff', '--worktree-raw', '--', './value.txt'],
        ['git', 'log', '-n', '1'],
    ]})
    value = text_result(wire)
    assert value['exit_code'] == 0, value
    assert [step['exit_code'] for step in value['steps']] == [0] * 8
    assert '[command 3/8]\nunstaged\n[command 4/8]\n2\tvalue.txt\n' in value['stdout']
    assert '+staged\n+extra' in value['stdout'] and '+second' in value['stdout']
    assert '-staged\n+unstaged\n extra' in value['stdout']
    assert value['stdout'].endswith(' second\n')
    assert not (source / 'driver-ran').exists()
    assert wire.meta is None


async def test_read_only_batch_does_not_follow_file_or_directory_symlinks(project):
    _, server, source, other = project
    (source / 'outside').symlink_to(other, target_is_directory=True)
    (source / 'link').symlink_to(other / 'value.txt')
    await call(server, 'a', 'workspace_switch', host='a', cwd=str(source))
    value = text_result(await wire_call(server, 'a', 'inspect_batch', command={
        'commands': [['ls', 'outside'], ['head', 'link'], ['wc', '-l', str(other / 'value.txt')]],
        'stop_on_error': False,
    }))
    assert [step['exit_code'] for step in value['steps']] == [1, 1, 1]
    assert 'other\n' not in value['stdout']


async def test_process_failure_remains_visible_in_text(project):
    _, server, source, _ = project
    text = text_result(await wire_call(server, 'a', 'exec_command', host='a', cwd=str(source),
        command=['python3', '-c', "import sys;print('problem',file=sys.stderr);sys.exit(7)"]))
    assert text['exit_code'] == 7 and text['stderr'] == 'problem\n'
    assert text['execution_state'] == 'finished'


@pytest.mark.parametrize('tool,state,timed_out', [
    ('exec_command', 'remote_state_unknown', False),
    ('exec_command', 'remote_state_unknown', True),
    ('exec_command', 'cancellation_requested', False),
    ('apply_patch', 'remote_state_unknown', False),
    ('apply_patch', 'remote_state_unknown', True),
])
async def test_unknown_zero_exit_survives_native_text(project, monkeypatch, tool, state, timed_out):
    router, server, source, _ = project
    await call(server, 'a', 'workspace_switch', host='a', cwd=str(source))
    executor = router.snapshot(router.registry.get_or_create('a')).workspace.executor
    original = executor.run
    dispatches = []
    async def lose_completion(*args, **kwargs):
        dispatches.append(args)
        result = await original(*args, **kwargs)
        task = dict(result.remote_task or {}, cleanup_state='unconfirmed')
        audit.update(result.output_id, execution_state=state, remote_task=task,
                     status='timeout' if timed_out else 'finished', timed_out=timed_out)
        return replace(result, execution_state=state, remote_task=task, timed_out=timed_out)
    monkeypatch.setattr(executor, 'run', lose_completion)
    if tool == 'exec_command':
        wire = await wire_call(server, 'a', tool, command=['printf', 'observed'], read_files=['value.txt'])
    else:
        wire = await wire_call(server, 'a', tool,
            patch='*** Begin Patch\n*** Update File: value.txt\n@@\n-original\n+observed\n*** End Patch',
            command=['printf', 'MUST_NOT_RUN'], read_files=['value.txt'])
        assert (source / 'value.txt').read_text() == 'observed\n'
    result = text_result(wire)
    assert result['execution_state'] == state
    with audit.scoped_root(router.audit_dir(router.registry.get_or_create('a').workspace_id)):
        record = json.loads((audit.directory(result['output_id']) / 'record.json').read_text())
    assert record['exit_code'] == 0 and record['execution_state'] == state
    assert record['remote_task']['cleanup_state'] == 'unconfirmed'
    if tool == 'exec_command':
        assert result['exit_code'] == 0 and result['remote_task']['cleanup_state'] == 'unconfirmed'
    assert bool(result.get('timed_out')) is timed_out
    assert 'files' not in result and 'command_result' not in result
    assert result.get('files_skipped') and len(dispatches) == 1


async def test_review_refusal_is_plain_text_without_execution(project, monkeypatch):
    router, server, source, _ = project
    await call(server, 'a', 'workspace_switch', host='a', cwd=str(source))
    executor = router.snapshot(router.registry.get_or_create('a')).workspace.executor
    async def forbidden(*args, **kwargs):
        raise AssertionError('review refusal must not dispatch SSH')
    monkeypatch.setattr(executor, 'run', forbidden)
    result = text_result(await wire_call(server, 'a', 'exec_command', command='make -j64 --help'))
    assert result['executed'] is False and result['error'] == 'compute_review_required'


async def test_card_preview_is_bounded_and_respects_the_text_budget(project):
    _, server, source, _ = project
    wire = await wire_call(server, 'a', 'exec_command', host='a', cwd=str(source),
        command=['python3', '-c', "print('x'*9000)"], max_bytes=256)
    result = text_result(wire)
    assert len(result['stdout'].encode()) <= 256 and result['truncated']
    review = await wire_call(server, 'a', 'review_activity', operation_ids=[result['operation_id']])
    preview = review.meta['remote-workbench/operation']['steps'][0]['streams']['stdout']
    assert len(preview['content'].encode()) <= inline_app.PREVIEW_BYTES and preview['truncated']
    full = text_result(await wire_call(server, 'a', 'read_output', output_id=result['output_id'], max_bytes=16384))
    assert 'x' * 9000 in full['content']


async def test_default_batch_preview_keeps_full_output_available(project):
    _, server, source, _ = project
    commands = {'commands': [['python3', '-c', "print('x'*7000)"], ['printf', 'last\n']]}
    args = dict(host='a', cwd=str(source), command=commands)
    result = text_result(await wire_call(server, 'a', 'exec_batch', **args))
    assert result['truncated'] and len(result['stdout'].encode()) <= 4096
    assert 'remote_task' not in result and result['exit_code'] == 0
    full = text_result(await wire_call(server, 'a', 'read_output', output_id=result['output_id']))
    assert 'x' * 7000 in full['content'] and full['content'].endswith('last\n')
    expanded = text_result(await wire_call(server, 'a', 'exec_batch', **args, max_bytes=8192))
    assert 'x' * 7000 in expanded['stdout'] and not expanded.get('truncated')


async def test_stdio_binds_activity_and_diff_cards_and_keeps_archived_resources(tmp_path):
    config = tmp_path / 'hosts.yaml'
    config.write_text('hosts:\n  test:\n    ssh_host: never-connect.invalid\n')
    params = StdioServerParameters(command=sys.executable, args=['-m','remote_workbench.session_server',
        '--config',str(config),'--data-dir',str(tmp_path/'state'),'--viewer-port','0'])
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as client:
            await client.initialize()
            tools = (await client.list_tools()).tools
            assert len(tools) == 12
            for tool in tools:
                meta = tool.meta or {}
                expected = inline_app.RESOURCE_URI if tool.name in inline_app.CARD_TOOLS else None
                assert meta.get('ui', {}).get('resourceUri') == expected
                assert meta.get('openai/outputTemplate') == expected
            batch = (await client.read_resource(inline_app.BATCH_RESOURCE_URI)).contents[0]
            assert batch.mimeType == 'text/html;profile=mcp-app'
            assert batch.meta['openai/widgetShowCodexWidgetInline'] is True
            content = (await client.read_resource(inline_app.RESOURCE_URI)).contents[0]
            assert content.mimeType == 'text/html;profile=mcp-app'
            assert content.meta['openai/widgetShowCodexWidgetInline'] is True
            assert content.meta['ui']['csp']['connectDomains'] == []
            for uri in inline_app.HISTORICAL_RESOURCE_URIS:
                historical = (await client.read_resource(uri)).contents[0]
                assert historical.text == content.text
                assert historical.meta['openai/widgetShowCodexWidgetInline'] is False
            result = await client.call_tool('exec_command', {'command':'must never run'}, meta={'threadId':'local'})
            assert result.isError and 'no remote workspace' in result.content[0].text


def test_batch_card_bridge_renders_the_text_result_on_demand():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node.js is needed for the batch card bridge harness')
    subprocess.run([node, str(Path(__file__).with_name('batch_card.cjs'))], check=True, timeout=20)


def test_archived_card_bridge_paging_and_untrusted_text():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node.js is needed for the archived UI bridge harness')
    subprocess.run([node, str(Path(__file__).with_name('inline_app.cjs'))], check=True, timeout=20)


def test_many_archived_cards_bound_background_and_repeated_render_work():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node.js is needed for the archived UI scheduling harness')
    subprocess.run([node, str(Path(__file__).with_name('inline_app_performance.cjs'))], check=True, timeout=20)
