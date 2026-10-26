import asyncio
import json
import sys

import pytest

from remote_workbench import ssh as ssh_module
from remote_workbench import remote_task
from tests.test_session_server import call as call, project as project
from tests.test_task_lifecycle import alive, wait_file


def _count_transport_launches(monkeypatch):
    original = ssh_module.asyncio.create_subprocess_exec
    launched = []

    async def counted(*args, **kwargs):
        launched.append(args)
        return await original(*args, **kwargs)

    monkeypatch.setattr(ssh_module.asyncio, "create_subprocess_exec", counted)
    return launched


def _python(code, *args):
    return [sys.executable, "-c", code, *args]


async def test_batch_runs_steps_in_one_transport_with_literal_argv(project, local, monkeypatch):
    router, server, source, _ = project
    await call(server, "a", "workspace_switch", host="a", cwd=str(source))
    _, root = local
    launches = _count_transport_launches(monkeypatch)
    order_path = root / "batch-order.txt"
    injection_target = root / "batch-injection.txt"
    literal_args = [
        'quote " and apostrophe \'',
        r"back\\slash $HOME",
        "$(touch {})".format(injection_target),
        "雪と火",
    ]
    first_code = (
        "import json,sys; from pathlib import Path; "
        "Path(sys.argv[1]).write_text('first'); "
        "print(json.dumps(sys.argv[2:], ensure_ascii=False))"
    )
    second_code = (
        "import sys; from pathlib import Path; p=Path(sys.argv[1]); "
        "assert p.read_text() == 'first'; p.write_text('first|second'); "
        "print('second-step')"
    )
    third_code = (
        "import sys; from pathlib import Path; p=Path(sys.argv[1]); "
        "assert p.read_text() == 'first|second'; p.write_text('first|second|third'); "
        "print('third-step')"
    )
    command = {
        "commands": [
            _python(first_code, str(order_path), *literal_args),
            _python(second_code, str(order_path)),
            _python(third_code, str(order_path)),
        ]
    }

    result = await call(server, "a", "exec_command", command=command)

    assert len(launches) == 1
    assert result["execution_state"] == "finished"
    assert result["exit_code"] == 0
    assert result["batch_state"] == "finished"
    assert [step["state"] for step in result["steps"]] == ["finished"] * 3
    assert [step["exit_code"] for step in result["steps"]] == [0, 0, 0]
    assert all(isinstance(step["elapsed_ms"], int) for step in result["steps"])
    assert json.dumps(literal_args, ensure_ascii=False) in result["stdout"]
    assert "second-step" in result["stdout"] and "third-step" in result["stdout"]
    assert order_path.read_text() == "first|second|third"
    assert not injection_target.exists()
    record = await call(server, 'a', 'audit_get', operation_id=result['output_id'])
    assert record['command_batch'] == command
    assert record['stdin_bytes'] == len(json.dumps(command, ensure_ascii=False).encode())
    assert 'batch' not in result['remote_task']  # No duplicated step payload.


@pytest.mark.parametrize(
    ("stop_on_error", "expected_states", "expected_output"),
    [
        (True, ["finished", "not_run", "not_run"], ["first-step"]),
        (False, ["finished", "finished", "finished"], ["first-step", "second-step", "third-step"]),
    ],
)
async def test_batch_stop_and_continue_semantics(
    project, stop_on_error, expected_states, expected_output
):
    _, server, source, _ = project
    await call(server, "a", "workspace_switch", host="a", cwd=str(source))
    command = {
        "commands": [
            _python("import sys; print('first-step'); sys.exit(7)"),
            _python("import sys; print('second-step'); sys.exit(4)"),
            _python("print('third-step')"),
        ],
        "stop_on_error": stop_on_error,
    }

    result = await call(server, "a", "exec_command", command=command)

    assert result["exit_code"] == 7
    assert result["batch_state"] == "finished"
    assert [step["state"] for step in result["steps"]] == expected_states
    assert [step["exit_code"] for step in result["steps"]] == (
        [7, None, None] if stop_on_error else [7, 4, 0]
    )
    for output in ("first-step", "second-step", "third-step"):
        assert (output in result["stdout"]) is (output in expected_output)


async def test_timeout_prevents_later_batch_sentinel(project, local):
    _, server, source, _ = project
    await call(server, "a", "workspace_switch", host="a", cwd=str(source))
    _, root = local
    sentinel = root / "after-timeout.txt"
    command = {
        "commands": [
            _python("import time; time.sleep(30)"),
            _python(
                "import sys; from pathlib import Path; "
                "Path(sys.argv[1]).write_text('launched')",
                str(sentinel),
            ),
        ]
    }

    result = await call(server, "a", "exec_command", command=command, timeout=2)

    assert result["timed_out"] is True
    assert result["exit_code"] == 124
    assert not sentinel.exists()
    assert result["batch_state"] == "unknown"
    if result["steps"]:
        assert len(result["steps"]) == 2
        assert result["steps"][0]["state"] in {"unknown", "not_run"}
        assert result["steps"][1]["state"] == "not_run"


async def test_batch_compute_and_auth_checks_reject_before_transport(project, monkeypatch):
    _, server, source, _ = project
    await call(server, "a", "workspace_switch", host="a", cwd=str(source))
    launches = _count_transport_launches(monkeypatch)

    compute = await call(
        server,
        "a",
        "exec_command",
        command={"commands": [["pytest", "-n", "8"]]},
    )
    auth = await call(
        server,
        "a",
        "exec_command",
        command={"commands": [["sshpass", "-p", "secret-marker", "ssh", "host"]]},
    )

    assert compute["error"] == "compute_review_required"
    assert compute["executed"] is False
    assert auth["error"] == "authentication_requires_user_terminal"
    assert auth["executed"] is False
    assert "secret-marker" not in json.dumps(auth)
    assert launches == []


async def test_batch_cancellation_cleans_active_step_and_retains_unknown_progress(local):
    executor, root = local
    pid_file, sentinel = root/'batch-pid', root/'after-cancel'
    command = {'commands': [
        _python('import os,time;from pathlib import Path;import sys;'
                'Path(sys.argv[1]).write_text(str(os.getpid()));time.sleep(15)', str(pid_file)),
        _python('from pathlib import Path;import sys;Path(sys.argv[1]).touch()', str(sentinel))]}
    running = asyncio.create_task(executor.run('argv batch', batch=True,
        input_data=json.dumps(command).encode(), timeout=20))
    pid = await wait_file(pid_file)
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(running, 7)
    assert not alive(pid) and not sentinel.exists()
    records = [json.loads(p.read_text()) for p in (root/'audit').glob('*/record.json')]
    task = next(r for r in records if r.get('kind') == 'command')['remote_task']
    assert task['batch_state'] == 'unknown'
    assert [r['state'] for r in task['batch']] == ['unknown', 'not_run']


@pytest.mark.parametrize('damage', ['identity', 'count', 'complete', 'not_object', 'bad_row'])
def test_unverified_batch_progress_stays_unknown(tmp_path, damage):
    row = dict(index=0, state='running', exit_code=None, elapsed_ms=0)
    value = dict(version=1, nonce='correct', complete=False, steps=[row])
    if damage == 'identity':
        value['nonce'] = 'other'
    elif damage == 'count':
        value['steps'].append(dict(row, index=1))
    elif damage == 'complete':
        value['complete'] = True
    elif damage == 'not_object':
        value = []
    else:
        value['steps'] = [None]
    (tmp_path/'batch-progress.json').write_text(json.dumps(value))
    assert remote_task.batch_progress(tmp_path, 'correct', 1) == {
        'batch_state': 'unknown', 'batch': [], 'batch_reason': 'progress_unavailable'}

