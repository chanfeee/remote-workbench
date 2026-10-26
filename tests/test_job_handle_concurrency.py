import asyncio
from contextlib import contextmanager
import json
import os
from pathlib import Path
import subprocess
import sys
import textwrap
import time
from types import SimpleNamespace

from remote_workbench import audit, job_collector


ROOT = Path(__file__).resolve().parents[1]
JOB_ID = "concurrent-job"


def _origin():
    return {
        "workspace_id": "workspace-test",
        "job_id": JOB_ID,
        "target_id": "target-test",
        "host": "simulated-host",
        "ssh_target": "local-simulation-only",
        "source_cwd": "/source",
        "target_cwd": "/target",
        "project_root": "/project",
        "origin_nonce": "a" * 32,
        "launch_cwd": "/target",
        "remote_state_dir": "/remote-state",
        "actual_cwd": "/target",
    }


def _remote_result(origin):
    job_dir = f"{origin['remote_state_dir']}/jobs/{JOB_ID}"
    return {
        "job_id": JOB_ID,
        "job_dir": job_dir,
        "running": True,
        "execution_state": "running",
        "remote_task": {
            "node": "local-simulation",
            "boot_id": "simulation-boot",
            "pid": 12345,
            "start_token": "simulation-start-token",
            "version": 2,
            "task_id": JOB_ID,
            "task_dir": job_dir,
            "origin_nonce": origin["origin_nonce"],
            "launch_cwd": origin["launch_cwd"],
            "actual_cwd": origin["actual_cwd"],
            "state": "running",
        },
    }


def _executor():
    return SimpleNamespace(
        config=SimpleNamespace(ssh_target="local-simulation-only", default_cwd="/target"),
        remote_state_dir="/remote-state",
    )


def _wait_for_paths(paths, workers, timeout=8):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if all(path.exists() for path in paths):
            return
        for worker in workers:
            if worker.poll() is not None:
                stdout, stderr = worker.communicate()
                raise AssertionError(
                    f"worker exited before its barrier (status={worker.returncode}): "
                    f"stdout={stdout!r}; stderr={stderr!r}"
                )
        time.sleep(0.01)
    raise AssertionError("local worker barrier timed out")


@contextmanager
def _two_workers(tmp_path, script, audit_root, origin, extra_args=()):
    control = tmp_path / "worker-control"
    ready_dir = control / "ready"
    ready_dir.mkdir(parents=True)
    go = control / "go"
    release = control / "release"
    worker_env = dict(os.environ)
    worker_env["REMOTE_WORKBENCH_AUDIT_DIR"] = str(audit_root)
    worker_env["PYTHONPATH"] = str(ROOT) + os.pathsep + worker_env.get("PYTHONPATH", "")
    workers = []
    try:
        for index in range(2):
            workers.append(subprocess.Popen(
                [sys.executable, "-c", script, str(audit_root), str(go),
                 str(ready_dir / str(index)), str(release), JOB_ID,
                 json.dumps(origin), str(index), *map(str, extra_args)],
                cwd=ROOT,
                env=worker_env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            ))
        _wait_for_paths([ready_dir / str(index) for index in range(2)], workers)
        yield {"workers": workers, "go": go, "release": release, "control": control}
    finally:
        go.touch(exist_ok=True)
        release.touch(exist_ok=True)
        for worker in workers:
            if worker.poll() is None:
                try:
                    worker.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    worker.terminate()
                    try:
                        worker.communicate(timeout=2)
                    except subprocess.TimeoutExpired:
                        worker.kill()
                        worker.communicate()


def _worker_job_records(audit_root):
    records = []
    for path in audit_root.glob("*/record.json"):
        record = json.loads(path.read_text(encoding="utf-8"))
        if record.get("kind") == "job" and record.get("job_id") == JOB_ID:
            records.append((path.parent, record))
    return records


TRACK_WORKER = textwrap.dedent("""
    import asyncio, json, sys, time
    from pathlib import Path
    from types import SimpleNamespace
    from remote_workbench import audit, job_collector

    audit_root, go, ready, release, job_id, origin_json, worker_id = sys.argv[1:8]
    origin = json.loads(origin_json)
    remote = {
        "job_id": job_id,
        "job_dir": f"{origin['remote_state_dir']}/jobs/{job_id}",
        "running": True,
        "execution_state": "running",
        "remote_task": {
            "node": "local-simulation", "boot_id": "simulation-boot",
            "pid": 12345, "start_token": "simulation-start-token", "version": 2,
            "task_id": job_id, "task_dir": f"{origin['remote_state_dir']}/jobs/{job_id}",
            "origin_nonce": origin["origin_nonce"], "launch_cwd": origin["launch_cwd"],
            "actual_cwd": origin["actual_cwd"], "state": "running",
        },
    }
    Path(ready).touch()
    deadline = time.monotonic() + 8
    while not Path(go).exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    if not Path(go).exists():
        raise SystemExit("start barrier timed out")

    executor = SimpleNamespace(
        config=SimpleNamespace(ssh_target="local-simulation-only", default_cwd=origin["target_cwd"]),
        remote_state_dir=origin["remote_state_dir"],
    )
    async def no_collection(executor, job_id, origin=None):
        return job_collector.local_info(executor, job_id)

    job_collector.ensure = no_collection
    with audit.scoped_root(audit_root):
        value = asyncio.run(job_collector.track(
            executor, remote, "simulated background command", origin["launch_cwd"], origin=origin
        ))
    print(json.dumps({"worker": worker_id, "job_output_id": value.get("job_output_id"),
                      "error": value.get("error")}), flush=True)
""")


ENSURE_WORKER = textwrap.dedent("""
    import asyncio, json, os, sys, time
    from pathlib import Path
    from types import SimpleNamespace
    from remote_workbench import audit, job_collector

    audit_root, go, ready, release, job_id, origin_json, worker_id, started_dir, returned_dir = sys.argv[1:10]
    origin = json.loads(origin_json)
    Path(ready).touch()
    deadline = time.monotonic() + 8
    while not Path(go).exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    if not Path(go).exists():
        raise SystemExit("start barrier timed out")

    class Input:
        def write(self, data):
            return len(data)
        async def drain(self):
            return None
        def close(self):
            return None

    class ErrorReader:
        async def read(self, size):
            return b""

    class OutputLines:
        def __aiter__(self):
            return self._lines()
        async def _lines(self):
            deadline = time.monotonic() + 8
            while not Path(release).exists() and time.monotonic() < deadline:
                await asyncio.sleep(0.01)
            if not Path(release).exists():
                raise TimeoutError("local follower release timed out")
            if False:
                yield b""

    class LocalFollower:
        def __init__(self):
            self.stdin = Input()
            self.stdout = OutputLines()
            self.stderr = ErrorReader()
            self.returncode = None
        async def wait(self):
            self.returncode = 0
            return 0
        def kill(self):
            self.returncode = -9

    executor = SimpleNamespace(
        config=SimpleNamespace(ssh_target="local-simulation-only", default_cwd=origin["target_cwd"]),
        remote_state_dir=origin["remote_state_dir"],
    )
    def local_follower_args(source):
        return ["local-simulated-follower"]
    executor._ssh_args = local_follower_args

    async def fake_create_subprocess_exec(*args, **kwargs):
        Path(started_dir, worker_id).write_text(json.dumps({"argv": args}), encoding="utf-8")
        return LocalFollower()
    asyncio.create_subprocess_exec = fake_create_subprocess_exec

    async def run():
        value = await job_collector.ensure(executor, job_id, origin=origin)
        Path(returned_dir, worker_id).write_text(json.dumps(value), encoding="utf-8")
        tasks = list(getattr(executor, "_log_collectors", {}).values())
        await asyncio.gather(*tasks, return_exceptions=True)
        return value

    with audit.scoped_root(audit_root):
        value = asyncio.run(asyncio.wait_for(run(), timeout=8))
    print(json.dumps({"worker": worker_id, "job_output_id": value.get("job_output_id"),
                      "sync_state": value.get("sync_state"), "error": value.get("error")}), flush=True)
""")


def test_cross_process_track_installs_one_stable_job_output_handle(tmp_path):
    audit_root = tmp_path / "audit"
    audit_root.mkdir()
    origin = _origin()

    with _two_workers(tmp_path, TRACK_WORKER, audit_root, origin) as pair:
        pair["go"].touch()
        values = []
        for worker in pair["workers"]:
            stdout, stderr = worker.communicate(timeout=8)
            assert worker.returncode == 0, stderr
            values.append(json.loads(stdout))

    ids = [value["job_output_id"] for value in values]
    assert all(ids) and ids[0] == ids[1]
    assert all(value["error"] is None for value in values)
    with audit.scoped_root(audit_root):
        records = _worker_job_records(audit_root)
        info = job_collector.local_info(_executor(), JOB_ID)
        assert info["job_output_id"] == ids[0]
        assert len(records) == 1
        output_dir, record = records[0]
        assert output_dir.name == ids[0]
        assert record["job_id"] == JOB_ID
        assert (output_dir / "stdout").read_bytes() == b""
        assert (output_dir / "stderr").read_bytes() == b""


def test_cross_process_ensure_starts_one_effective_collector(tmp_path, monkeypatch):
    audit_root = tmp_path / "audit"
    audit_root.mkdir()
    origin = _origin()
    executor = _executor()

    async def no_collection(executor, job_id, origin=None):
        return job_collector.local_info(executor, job_id)

    monkeypatch.setattr(job_collector, "ensure", no_collection)
    with audit.scoped_root(audit_root):
        installed = asyncio.run(job_collector.track(
            executor, _remote_result(origin), "simulated background command",
            origin["launch_cwd"], origin=origin,
        ))
    output_id = installed["job_output_id"]
    assert output_id
    # Children import a fresh collector implementation and compete through
    # the real per-output flock. Their fake subprocess API is local-only.
    started_dir = tmp_path / "collector-started"
    returned_dir = tmp_path / "ensure-returned"
    started_dir.mkdir()
    returned_dir.mkdir()

    with _two_workers(tmp_path, ENSURE_WORKER, audit_root, origin,
                      extra_args=(started_dir, returned_dir)) as pair:
        pair["go"].touch()
        _wait_for_paths([returned_dir / str(index) for index in range(2)], pair["workers"])
        started = list(started_dir.iterdir())
        assert len(started) == 1
        assert json.loads(started[0].read_text(encoding="utf-8"))["argv"][0] == "local-simulated-follower"
        pair["release"].touch()
        values = []
        for worker in pair["workers"]:
            stdout, stderr = worker.communicate(timeout=8)
            assert worker.returncode == 0, stderr
            values.append(json.loads(stdout))

    assert [value["job_output_id"] for value in values] == [output_id, output_id]
    with audit.scoped_root(audit_root):
        records = _worker_job_records(audit_root)
        assert len(records) == 1 and records[0][0].name == output_id
        assert audit.read_output(output_id, "stdout")["content"] == ""
        assert audit.read_output(output_id, "stderr")["content"] == ""
