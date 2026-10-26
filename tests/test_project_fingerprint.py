from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from remote_workbench import audit
from remote_workbench.baseline import project_fingerprint
from remote_workbench.ssh import SSHResult


class LocalExecutor:
    """Run the stdlib-only remote scripts locally against temporary projects."""

    config = SimpleNamespace(file_timeout=10)

    async def run_program(self, argv, *, cwd, output_budget, timeout):
        result = subprocess.run(
            [sys.executable, *argv[1:]], cwd=cwd, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, timeout=timeout,
        )
        return SSHResult(
            stdout=result.stdout.decode("utf-8", "replace"),
            stderr=result.stderr.decode("utf-8", "replace"),
            exit_code=result.returncode,
        )


















@pytest.mark.asyncio
async def test_project_fingerprint_is_bounded_and_does_not_follow_external_symlink(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    (root / "code.py").write_text("safe code\n", encoding="utf-8")
    (root / ".venv").mkdir()
    (root / ".venv" / "ignored.py").write_text("ignored\n", encoding="utf-8")
    external = tmp_path / "external"
    external.mkdir()
    secret = external / "secret.txt"
    secret.write_text("secret version one", encoding="utf-8")
    os.symlink(external, root / "outside-link")
    executor = LocalExecutor()

    first = await project_fingerprint(executor, str(root), max_files=10, max_bytes=1024)
    secret.write_text("secret version two", encoding="utf-8")
    second = await project_fingerprint(executor, str(root), max_files=10, max_bytes=1024)
    assert first["complete"] is True and first["digest"] == second["digest"]
    assert first["files"] == 2
    assert "secret.txt" not in str(first)
    assert "secret version" not in str(first)

    too_small = await project_fingerprint(executor, str(root), max_files=10, max_bytes=2)
    assert too_small["complete"] is False
    assert too_small["digest"] is None
    assert too_small["reason"] == "byte_budget_exceeded"
