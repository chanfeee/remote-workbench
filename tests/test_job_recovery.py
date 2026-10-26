import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from remote_workbench import audit, job_collector, remote_task, remote_worker
from remote_workbench.tools import commands
from remote_workbench.workspace_registry import Registry, WorkspaceTarget


def recovery_origin(tmp_path, job_id="job-recover"):
    state_dir = str(tmp_path / "remote-state")
    target_cwd = str(tmp_path / "project")
    return {
        "workspace_id": "a" * 32,
        "job_id": job_id,
        "target_id": "b" * 32,
        "host": "a",  # Binding alias, deliberately distinct from the SSH endpoint.
        "ssh_target": "host-a",
        "source_cwd": str(tmp_path / "source"),
        "target_cwd": target_cwd,
        "project_root": target_cwd,
        "origin_nonce": "c" * 32,
        "launch_cwd": target_cwd,
        "actual_cwd": None,
        "remote_state_dir": state_dir,
    }


def worker_result(origin, *, nonce=None):
    job_dir = str(Path(origin["remote_state_dir"]) / "jobs" / origin["job_id"])
    task = {
        "version": 2,
        "node": "node-a",
        "boot_id": "boot-a",
        "pid": 456,
        "start_token": "proc:token-a",
        "task_id": origin["job_id"],
        "task_dir": job_dir,
        "origin_nonce": nonce or origin["origin_nonce"],
        "launch_cwd": origin["launch_cwd"],
        "actual_cwd": origin["launch_cwd"],
        "state": "running",
    }
    return {"job_id": origin["job_id"], "job_dir": job_dir, "remote_task": task,
            "running": True, "exit_code": None, "execution_state": "running"}


def executor_for(origin):
    return SimpleNamespace(
        config=SimpleNamespace(ssh_target=origin["ssh_target"], default_cwd=origin["target_cwd"]),
        remote_state_dir=origin["remote_state_dir"],
    )


async def test_recovery_installs_one_stable_handle_and_never_resubmits(tmp_path, monkeypatch):
    monkeypatch.setenv("REMOTE_WORKBENCH_AUDIT_DIR", str(tmp_path / "audit"))
    origin = recovery_origin(tmp_path)
    executor = executor_for(origin)
    calls = []

    async def read_only_status(executor_arg, payload, **kwargs):
        calls.append(dict(payload))
        assert payload["op"] == "job_status"
        assert payload["job_id"] == origin["job_id"]
        assert payload["expected_origin_nonce"] == origin["origin_nonce"]
        return worker_result(origin)

    stopped = asyncio.Event()

    async def hold_collector(executor_arg, path):
        await stopped.wait()

    monkeypatch.setattr("remote_workbench.remote_client.edit", read_only_status)
    monkeypatch.setattr(job_collector, "_collect", hold_collector)

    with audit.scoped_root(tmp_path / "audit"):
        first = await job_collector.ensure(executor, origin["job_id"], origin=origin)
        second = await job_collector.ensure(executor, origin["job_id"], origin=origin)
        assert first["job_output_id"] == second["job_output_id"]
        assert first["execution_state"] == "running"
        assert len(calls) == 1
        assert calls[0]["op"] != "job_start"
        assert job_collector.local_info(executor, origin["job_id"])["actual_cwd"] == origin["launch_cwd"]
        await job_collector.close(executor)


@pytest.mark.parametrize("failure", ["nonce", "connection"])
async def test_recovery_failure_returns_unknown_without_creating_a_handle(tmp_path, monkeypatch, failure):
    monkeypatch.setenv("REMOTE_WORKBENCH_AUDIT_DIR", str(tmp_path / "audit"))
    origin = recovery_origin(tmp_path)
    executor = executor_for(origin)

    async def fail_or_mismatch(executor_arg, payload, **kwargs):
        if failure == "connection":
            raise OSError("local simulated SSH channel unavailable")
        return worker_result(origin, nonce="d" * 32)

    monkeypatch.setattr("remote_workbench.remote_client.edit", fail_or_mismatch)
    with audit.scoped_root(tmp_path / "audit"):
        result = await job_collector.ensure(executor, origin["job_id"], origin=origin)
        assert result["execution_state"] == "unknown"
        assert result["error"] == "job_identity_unconfirmed"
        if failure == "nonce":
            assert result["state_reason"] == "origin_nonce_mismatch"
        else:
            assert result["state_reason"].startswith("recovery_connection_unavailable:")
        assert job_collector.local_info(executor, origin["job_id"])["error"] == "local_job_not_found"
        assert not getattr(executor, "_log_collectors", {})


def test_terminal_origin_is_verified_without_requiring_pid_to_remain_alive(tmp_path, monkeypatch):
    monkeypatch.setattr(remote_task, "machine_identity", lambda: {"node": "node-a", "boot_id": "boot-a"})
    monkeypatch.setattr(remote_task, "process_start",
                        lambda pid: (_ for _ in ()).throw(AssertionError("terminal PID must not be probed")))
    job_id = "finished"
    state_dir = str(tmp_path / "state")
    job_dir = str(Path(state_dir) / "jobs" / job_id)
    origin = {"version": 2, "node": "node-a", "boot_id": "boot-a", "pid": 42,
              "start_token": "proc:dead-but-persisted", "task_id": job_id,
              "task_dir": job_dir, "origin_nonce": "e" * 32,
              "launch_cwd": str(tmp_path), "actual_cwd": str(tmp_path)}
    status = dict(origin, state="finished", exit_code=0)

    valid, reason, running = remote_task.verify_job_origin(
        origin, status, job_id=job_id, remote_state_dir=state_dir,
        expected_nonce=origin["origin_nonce"], expected_launch_cwd=origin["launch_cwd"],
        expected_actual_cwd=origin["actual_cwd"], exit_code=0)

    assert valid and reason is None and running is False


def test_legacy_complete_local_handle_can_still_be_safely_controlled(tmp_path, monkeypatch):
    origin = recovery_origin(tmp_path, "legacy-job")
    executor = executor_for(origin)
    remote_state_dir = origin["remote_state_dir"]
    origin.update(origin_nonce=None, launch_cwd=None, remote_state_dir=None, ssh_target=None)
    launch_cwd = str(tmp_path / "project")
    job_dir = str(Path(remote_state_dir) / "jobs" / origin["job_id"])
    task = {
        "version": 2, "node": "node-a", "boot_id": "boot-a", "pid": 456,
        "start_token": "proc:legacy-token", "task_id": origin["job_id"],
        "task_dir": job_dir, "state": "finished",
    }
    captured = []

    async def control(job_id, *, origin=None, kind="jobs"):
        captured.append((job_id, origin))
        return {"job_id": job_id, "cancel_requested": False}

    executor.cancel_task = control
    monkeypatch.setattr(commands, "get_executor", lambda host=None: executor)
    with audit.scoped_root(tmp_path / "audit"):
        output_id = audit.create(
            kind="job", host=executor.config.ssh_target, cwd=launch_cwd,
            remote_job_dir=job_dir, remote_task=task,
            workspace_id=origin["workspace_id"], target_id=origin["target_id"],
            running=False, exit_code=0, execution_state="finished", sync_state="complete",
            status="finished",
        )
        for stream in ("stdout", "stderr"):
            with audit.open_stream(output_id, stream):
                pass
        handle_path = job_collector._handle_path(executor, origin["job_id"])
        handle_path.parent.mkdir(parents=True, exist_ok=True)
        handle_path.write_text(json.dumps({
            "job_id": origin["job_id"], "job_dir": job_dir,
            "job_output_id": output_id, "remote_task": task,
        }))
        with job_collector.scoped_origin(origin):
            result = asyncio.run(commands.ssh_stop_job(origin["job_id"]))

    assert result["cancel_requested"] is False
    assert len(captured) == 1
    saved_origin = captured[0][1]
    assert saved_origin["host"] == "a"
    assert saved_origin["ssh_target"] == "host-a"
    assert saved_origin["launch_cwd"] == launch_cwd
    assert saved_origin["remote_state_dir"] == remote_state_dir
    assert saved_origin["_legacy_identity"] == {
        key: task[key] for key in
        ("node", "boot_id", "pid", "start_token", "task_id", "task_dir", "version")
    }


def test_status_recovery_rejects_symlinked_log_before_reading_output(tmp_path, monkeypatch):
    state_dir = tmp_path / "remote-state"
    job_id = "symlinked-log"
    job_dir = state_dir / "jobs" / job_id
    job_dir.mkdir(parents=True)
    nonce = "e" * 32
    launch_cwd = str(tmp_path / "project")
    identity = {
        "version": 2, "node": "node-a", "boot_id": "boot-a", "pid": 456,
        "start_token": "proc:symlink-token", "task_id": job_id,
        "task_dir": str(job_dir), "origin_nonce": nonce,
        "launch_cwd": launch_cwd, "actual_cwd": launch_cwd,
    }
    (job_dir / "origin.json").write_text(json.dumps(identity))
    (job_dir / "status.json").write_text(json.dumps(dict(identity, state="running")))
    (job_dir / "stderr.log").write_bytes(b"")
    target = tmp_path / "outside.log"
    target.write_text("untrusted bytes\n")
    (job_dir / "stdout.log").symlink_to(target)
    monkeypatch.setattr(remote_worker, "state_root", lambda: state_dir)
    monkeypatch.setattr(remote_worker, "verify_job_origin", lambda *args, **kwargs: (True, None, True))

    result = remote_worker.job({
        "op": "job_status", "job_id": job_id, "expected_origin_nonce": nonce,
        "expected_launch_cwd": launch_cwd, "expected_remote_state_dir": str(state_dir),
        "stdout_cursor": 0, "stderr_cursor": 0, "budget": 256, "tail_lines": 1,
    })

    assert result["execution_state"] == "unknown"
    assert result["state_reason"] == "job_identity_path_is_symlink"


def test_registry_persists_original_alias_endpoint_paths_and_verified_cwd(tmp_path):
    registry_path = tmp_path / "registry.sqlite3"
    registry = Registry(registry_path)
    state = registry.get_or_create("thread")
    target = WorkspaceTarget("b" * 32, state.workspace_id, "a", str(tmp_path / "source"),
                             str(tmp_path / "project"), str(tmp_path / "project"))
    registry.commit_switch(state.workspace_id, 0, target)
    launch_cwd = str(tmp_path / "project" / "link")
    remote_state_dir = str(tmp_path / "project" / ".remote-workbench-state")
    operation, created = registry.reserve_job_operation(
        state.workspace_id, "request", target, "job", "f" * 64,
        origin_nonce="c" * 32, launch_cwd=launch_cwd,
        remote_state_dir=remote_state_dir, ssh_target="host-a")
    assert created and operation.target.host == "a" and operation.ssh_target == "host-a"

    registry.update_job_actual_cwd(state.workspace_id, "job", str(tmp_path / "physical-project"))
    restarted = Registry(registry_path)
    saved = restarted.get_job_operation(state.workspace_id, "request")
    assert saved.launch_cwd == launch_cwd
    assert saved.remote_state_dir == remote_state_dir
    assert saved.actual_cwd == str(tmp_path / "physical-project")
    with pytest.raises(ValueError, match="conflicts"):
        restarted.update_job_actual_cwd(state.workspace_id, "job", str(tmp_path / "other"))
