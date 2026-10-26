"""Legacy D3 rows remain untouched by ordinary background job collection."""
import asyncio
import json
import uuid
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from remote_workbench import audit, background_test_evidence, job_collector
from remote_workbench.tools import commands
from remote_workbench.workspace_registry import Registry, WorkspaceTarget


async def eventually(predicate, timeout=12):
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.05)
    raise AssertionError("background job did not reach the expected state")


def seed_legacy_history(root, project, job_id):
    source = root / "source"
    source.mkdir(exist_ok=True)
    registry = Registry(root / "registry.sqlite3")
    state = registry.get_or_create("legacy-background-history")
    target = WorkspaceTarget(uuid.uuid4().hex, state.workspace_id, "a", str(source),
                             str(project), str(project))
    registry.commit_switch(state.workspace_id, state.revision, target)
    legacy_test = {
        "command": "pytest -q", "operation_id": uuid.uuid4().hex,
        "status": "passed", "applies_to_current_code": True,
    }
    registry.save_test(state.workspace_id, target.target_id, legacy_test)
    attempt = registry.begin_test_attempt(
        state.workspace_id, target, attempt_id=uuid.uuid4().hex, job_id=job_id,
        command="pytest -q", cwd=str(project), execution_kind="background",
        started_at=1.0, before={"code": {"digest": "legacy-only"}},
        revision=state.revision + 1,
    )
    attempt = registry.test_attempt(state.workspace_id, attempt["attempt_id"])
    return registry, state, target, legacy_test, attempt


def unchanged(registry, state, target, legacy_test, attempt):
    assert registry.last_test(state.workspace_id, target.target_id) == legacy_test
    assert registry.test_attempt(state.workspace_id, attempt["attempt_id"]) == attempt


def block_legacy_collector_hook(monkeypatch):
    hook = AsyncMock(side_effect=AssertionError("legacy evidence hook called by normal collector"))
    monkeypatch.setattr(background_test_evidence, "record_collector_event", hook)
    return hook


@pytest.mark.parametrize(("fails", "exit_code"), [(False, 0), (True, 1)])
@pytest.mark.asyncio
async def test_background_completion_preserves_output_and_does_not_update_legacy_rows(
        local, monkeypatch, fails, exit_code):
    executor, root = local
    project = root / "project"
    project.mkdir()
    executor.config.default_cwd = str(project)
    executor.remote_state_dir = str(root / "remote-state")
    assertion = "self.fail('expected failure')" if fails else "self.assertTrue(True)"
    (project / "test_case.py").write_text(
        "import time, unittest\n"
        "class TestRun(unittest.TestCase):\n"
        "    def test_result(self):\n"
        "        time.sleep(0.3)\n"
        f"        {assertion}\n"
    )
    job_id = "legacy-bg-" + uuid.uuid4().hex[:12]
    history = seed_legacy_history(root, project, job_id)
    hook = block_legacy_collector_hook(monkeypatch)
    command = "python3 -m unittest -v test_case"

    launched = await commands.ssh_run_background(command, cwd=str(project), job_id=job_id,
                                                  timeout=15, max_bytes=8192)
    output_id = launched["job_output_id"]
    try:
        await eventually(lambda: executor.job_local_info(job_id).get("sync_state") == "complete")
        record = json.loads((audit.directory(output_id) / "record.json").read_text())
        assert record["exit_code"] == exit_code
        assert "Ran 1 test" in audit.read_output(output_id, "stderr")["content"]
        assert "pytest" in history[3]["command"]
        unchanged(*history)
        assert hook.await_count == 0
    finally:
        await executor.close_collectors()


@pytest.mark.asyncio
async def test_stopping_background_job_keeps_legacy_rows_unchanged(local, monkeypatch):
    executor, root = local
    project = root / "project"
    project.mkdir()
    executor.config.default_cwd = str(project)
    executor.remote_state_dir = str(root / "remote-state")
    ready, release = root / "ready", root / "release"
    module = project / "test_case.py"
    module.write_text(
        "import os, time, unittest\n"
        "from pathlib import Path\n"
        "Path(os.environ['RWB_READY']).write_text('ready')\n"
        "gate = Path(os.environ['RWB_RELEASE'])\n"
        "while not gate.exists(): time.sleep(0.02)\n"
        "class TestRun(unittest.TestCase):\n"
        "    def test_result(self): self.assertTrue(True)\n"
    )
    command = (f"RWB_READY={ready} RWB_RELEASE={release} "
               "python3 -m unittest -v test_case")
    job_id = "legacy-stop-" + uuid.uuid4().hex[:12]
    history = seed_legacy_history(root, project, job_id)
    hook = block_legacy_collector_hook(monkeypatch)
    launched = await commands.ssh_run_background(command, cwd=str(project), job_id=job_id,
                                                  timeout=20, max_bytes=8192)
    try:
        await eventually(ready.exists)
        stopped = await commands.ssh_stop_job(job_id)
        assert stopped.get("execution_state") == "finished", stopped
        await eventually(lambda: executor.job_local_info(job_id).get("sync_state") == "complete")
        assert executor.job_local_info(job_id)["job_output_id"] == launched["job_output_id"]
        unchanged(*history)
        assert hook.await_count == 0
    finally:
        release.touch(exist_ok=True)
        await executor.close_collectors()


@pytest.mark.asyncio
async def test_reconnected_collector_keeps_legacy_rows_unchanged(local, monkeypatch):
    executor, root = local
    project = root / "project"
    project.mkdir()
    executor.config.default_cwd = str(project)
    executor.remote_state_dir = str(root / "remote-state")
    ready, release = root / "ready", root / "release"
    module = project / "test_case.py"
    module.write_text(
        "import os, time, unittest\n"
        "from pathlib import Path\n"
        "Path(os.environ['RWB_READY']).write_text('ready')\n"
        "gate = Path(os.environ['RWB_RELEASE'])\n"
        "while not gate.exists(): time.sleep(0.02)\n"
        "class TestRun(unittest.TestCase):\n"
        "    def test_result(self): self.assertTrue(True)\n"
    )
    command = (f"RWB_READY={ready} RWB_RELEASE={release} "
               "python3 -m unittest -v test_case")
    job_id = "legacy-reconnect-" + uuid.uuid4().hex[:12]
    history = seed_legacy_history(root, project, job_id)
    hook = block_legacy_collector_hook(monkeypatch)
    launched = await commands.ssh_run_background(command, cwd=str(project), job_id=job_id,
                                                  timeout=20, max_bytes=8192)
    try:
        await eventually(ready.exists)
        await job_collector.close(executor)
        await eventually(lambda: executor.job_local_info(job_id).get("sync_state") == "disconnected")
        assert executor.job_local_info(job_id)["execution_state"] == "unknown"

        restored = await executor.ensure_job_collection(job_id)
        assert restored["job_output_id"] == launched["job_output_id"]
        release.touch(exist_ok=True)
        await eventually(lambda: executor.job_local_info(job_id).get("sync_state") == "complete")
        assert "Ran 1 test" in audit.read_output(launched["job_output_id"], "stderr")["content"]
        unchanged(*history)
        assert hook.await_count == 0
    finally:
        release.touch(exist_ok=True)
        await executor.close_collectors()
