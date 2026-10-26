import json
import stat
import sys

import pytest

from remote_workbench.batch_runner import run_batch


def _python_command(code, *args):
    return [sys.executable, "-c", code, *args]


def _status(path):
    return json.loads(path.read_text(encoding="utf-8"))


def test_argv_is_literal_and_child_output_is_inherited(tmp_path, capfd):
    injection_target = tmp_path / "should-not-exist"
    args = [
        'a "quoted" value',
        r"back\\slash $HOME",
        "$(touch {})".format(injection_target),
        "雪と火",
    ]
    code = (
        "import json,sys; "
        "print(json.dumps(sys.argv[1:], ensure_ascii=False)); "
        "print('stderr-marker', file=sys.stderr)"
    )

    result = run_batch(
        {"commands": [_python_command(code, *args)]},
        tmp_path / "status.json",
        "literal-nonce",
    )

    captured = capfd.readouterr()
    assert result == 0
    assert captured.out.splitlines() == [
        "[command 1/1]",
        json.dumps(args, ensure_ascii=False),
    ]
    assert "stderr-marker" in captured.err
    assert not injection_target.exists()


def test_progress_is_private_and_running_is_written_before_spawn(tmp_path, capfd):
    status_path = tmp_path / "status.json"
    code = (
        "import json,sys; "
        "status=json.load(open(sys.argv[1], encoding='utf-8')); "
        "print(json.dumps(status['steps'][0]))"
    )

    result = run_batch(
        {"commands": [_python_command(code, str(status_path))]},
        status_path,
        "nonce-雪",
    )

    captured = capfd.readouterr()
    observed = json.loads(captured.out.splitlines()[1])
    final_status = _status(status_path)
    assert result == 0
    assert observed == {
        "index": 0,
        "state": "running",
        "exit_code": None,
        "elapsed_ms": 0,
    }
    assert final_status["version"] == 1
    assert final_status["nonce"] == "nonce-雪"
    assert final_status["complete"] is True
    assert final_status["steps"][0]["state"] == "finished"
    assert final_status["steps"][0]["exit_code"] == 0
    assert isinstance(final_status["steps"][0]["elapsed_ms"], int)
    assert stat.S_IMODE(status_path.stat().st_mode) == 0o600
    assert list(tmp_path.glob(".batch-status-*.tmp")) == []


def test_stop_on_error_marks_unstarted_commands_not_run(tmp_path, capfd):
    status_path = tmp_path / "status.json"
    commands = [
        _python_command("import sys; print('first-ran'); sys.exit(7)"),
        _python_command("print('must-not-run')"),
    ]

    result = run_batch({"commands": commands}, status_path, "stop")

    captured = capfd.readouterr()
    status = _status(status_path)
    assert result == 7
    assert "first-ran" in captured.out
    assert "must-not-run" not in captured.out
    assert status["complete"] is True
    assert [step["state"] for step in status["steps"]] == ["finished", "not_run"]
    assert [step["exit_code"] for step in status["steps"]] == [7, None]


def test_continue_on_error_returns_first_nonzero_code(tmp_path):
    status_path = tmp_path / "status.json"
    commands = [
        _python_command("import sys; sys.exit(9)"),
        _python_command("import sys; sys.exit(4)"),
        _python_command("pass"),
    ]

    result = run_batch(
        {"commands": commands, "stop_on_error": False}, status_path, "continue"
    )

    status = _status(status_path)
    assert result == 9
    assert status["complete"] is True
    assert [step["state"] for step in status["steps"]] == [
        "finished",
        "finished",
        "finished",
    ]
    assert [step["exit_code"] for step in status["steps"]] == [9, 4, 0]


def test_missing_executable_is_127_with_bounded_error_and_facts(tmp_path):
    status_path = tmp_path / "status.json"
    result = run_batch(
        {
            "commands": [
                [str(tmp_path / "missing executable")],
                _python_command("print('must-not-run')"),
            ]
        },
        status_path,
        "missing",
    )

    status = _status(status_path)
    failed_step = status["steps"][0]
    assert result == 127
    assert failed_step["state"] == "finished"
    assert failed_step["exit_code"] == 127
    assert "FileNotFoundError" in failed_step["error"]
    assert "missing executable" in failed_step["error"]
    assert len(failed_step["error"]) <= 512
    assert status["steps"][1]["state"] == "not_run"


@pytest.mark.parametrize(
    "spec",
    [
        None,
        {},
        {"commands": "echo"},
        {"commands": []},
        {"commands": [[]]},
        {"commands": [[""]]},
        {"commands": [["echo", 3]]},
        {"commands": [["echo", "bad\x00arg"]]},
        {"commands": [("echo",)]},
        {"commands": [["echo"]], "stop_on_error": 1},
        {"commands": [["echo"]], "unexpected": True},
        {"commands": [["echo"]] * 33},
    ],
)
def test_invalid_specs_are_rejected_before_status_write(tmp_path, spec):
    status_path = tmp_path / "status.json"

    with pytest.raises(ValueError):
        run_batch(spec, status_path, "invalid")

    assert not status_path.exists()

