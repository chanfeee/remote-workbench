"""Run a small, sequential batch of argv commands with durable progress."""

import json
import os
import subprocess
import sys
import tempfile
import time


_MAX_COMMANDS = 32
_MAX_ERROR_CHARS = 512


def _validate_spec(spec):
    if not isinstance(spec, dict):
        raise ValueError("spec must be a dictionary")

    keys = set(spec)
    if "commands" not in keys or keys - {"commands", "stop_on_error"}:
        raise ValueError("spec must contain commands and optional stop_on_error only")

    commands = spec["commands"]
    if not isinstance(commands, list) or not 1 <= len(commands) <= _MAX_COMMANDS:
        raise ValueError("commands must be a list containing 1 to 32 argv lists")

    for index, argv in enumerate(commands):
        if not isinstance(argv, list) or not argv:
            raise ValueError("command {} must be a nonempty argv list".format(index))
        for arg in argv:
            if not isinstance(arg, str):
                raise ValueError("command {} arguments must be strings".format(index))
            if "\x00" in arg:
                raise ValueError("command {} arguments must not contain NUL".format(index))
        if not argv[0]:
            raise ValueError("command {} program must be nonempty".format(index))

    stop_on_error = spec.get("stop_on_error", True)
    if type(stop_on_error) is not bool:
        raise ValueError("stop_on_error must be a boolean")

    return commands, stop_on_error


def _write_status(status_path, status):
    """Atomically replace the status file with a private JSON snapshot."""
    target = os.path.abspath(os.fsdecode(os.fspath(status_path)))
    directory = os.path.dirname(target)
    descriptor, temporary_path = tempfile.mkstemp(
        prefix=".batch-status-", suffix=".tmp", dir=directory
    )
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            descriptor = -1
            json.dump(status, stream, ensure_ascii=False, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, target)
    except Exception:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            os.unlink(temporary_path)
        except OSError:
            pass
        raise


def _new_step(index):
    return {
        "index": index,
        "state": "not_started",
        "exit_code": None,
        "elapsed_ms": 0,
    }


def _elapsed_ms(started_at):
    return max(0, (time.monotonic_ns() - started_at) // 1000000)


def _bounded_oserror(error):
    """Return bounded, useful details about a command that could not start."""
    details = "{}: {}".format(type(error).__name__, error)
    if len(details) <= _MAX_ERROR_CHARS:
        return details
    return details[: _MAX_ERROR_CHARS - 3] + "..."


def run_batch(spec, status_path, nonce):
    """Run validated argv commands in order and return the batch exit code.

    Child processes inherit this process's stdout, stderr, and process group.
    Their stdin is closed.  Progress is replaced atomically before a spawn and
    after each completed step so a supervising process can inspect an
    incomplete run after cancellation or timeout.
    """
    commands, stop_on_error = _validate_spec(spec)
    status = {
        "version": 1,
        "nonce": nonce,
        "complete": False,
        "steps": [_new_step(index) for index in range(len(commands))],
    }
    _write_status(status_path, status)

    first_nonzero = 0
    for index, argv in enumerate(commands):
        step = status["steps"][index]
        step["state"] = "running"
        _write_status(status_path, status)

        sys.stdout.write("[command {}/{}]\n".format(index + 1, len(commands)))
        sys.stdout.flush()

        started_at = time.monotonic_ns()
        try:
            process = subprocess.Popen(
                argv,
                stdin=subprocess.DEVNULL,
                shell=False,
            )
        except OSError as error:
            exit_code = 127
            step["error"] = _bounded_oserror(error)
            step["state"] = "finished"
            step["exit_code"] = exit_code
            step["elapsed_ms"] = _elapsed_ms(started_at)
            _write_status(status_path, status)
            if first_nonzero == 0:
                first_nonzero = exit_code
            if stop_on_error:
                break
            continue

        raw_exit_code = process.wait()
        if raw_exit_code < 0:
            signal_number = -raw_exit_code
            exit_code = 128 + signal_number
            step["signal"] = signal_number
        else:
            exit_code = raw_exit_code
        step["state"] = "finished"
        step["exit_code"] = exit_code
        step["elapsed_ms"] = _elapsed_ms(started_at)
        _write_status(status_path, status)

        if exit_code != 0:
            if first_nonzero == 0:
                first_nonzero = exit_code
            if stop_on_error:
                break

    if stop_on_error and first_nonzero != 0:
        for step in status["steps"]:
            if step["state"] == "not_started":
                step["state"] = "not_run"

    status["complete"] = True
    _write_status(status_path, status)
    return first_nonzero

