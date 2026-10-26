"""Persistent thread workspaces and immutable, request-scoped SSH targets."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
import fcntl
import json
import os
from pathlib import Path
import posixpath
import uuid

from remote_workbench.resource_claims import ResourceClaims
from remote_workbench.config import ServerConfig
from remote_workbench.ssh import SSHExecutor
from remote_workbench.workspace import Workspace
from remote_workbench.workspace_registry import Registry, RevisionConflict, WorkspaceState, WorkspaceTarget


class WorkspaceUnbound(ValueError):
    pass


def default_state_dir() -> Path:
    """Stable local storage independent of the source or installed wheel."""
    configured = os.environ.get("REMOTE_WORKBENCH_DATA_DIR")
    if configured:
        return Path(configured).expanduser()
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "remote-workbench"


@dataclass(frozen=True)
class ExecutionSnapshot:
    thread_id: str
    workspace_id: str
    host: str
    cwd: str
    source_cwd: str
    project_root: str
    target_id: str
    revision: int
    workspace: Workspace
    directory_mode: str = "direct"

    def metadata(self):
        return dict(thread_id=self.thread_id, workspace_id=self.workspace_id,
                    revision=self.revision, target_id=self.target_id, directory_mode=self.directory_mode)


class SessionRouter:
    def __init__(self, config: ServerConfig, state_dir: Path, *, executor_factory=SSHExecutor):
        self.config = config
        self.state_dir = Path(state_dir).expanduser().resolve()
        self.state_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
        self.registry = Registry(self.state_dir / "workspaces.sqlite3")
        self.resources = ResourceClaims(self.state_dir)
        self.executor_factory = executor_factory
        self._executors = {}
        (self.state_dir / "locks").mkdir(mode=0o700, exist_ok=True)

    def audit_dir(self, workspace_id: str) -> Path:
        return self.state_dir / "audit" / workspace_id

    def requires_directory_selection(self, state):
        if state.host is None:
            return False
        return (not self.registry.direct_state_dir(state.target_id) or
                '.remote-workbench-sessions' in posixpath.normpath(state.cwd or '/').split('/'))

    @asynccontextmanager
    async def lease(self, workspace_id: str):
        """Serialize foreground calls and switches across local MCP processes.

        Nonblocking flock polling is cancellable and never blocks the event
        loop. Different conversations use different locks and run in parallel.
        """
        # Only registry-issued UUIDs may become local path components.
        if len(workspace_id) != 32 or any(c not in "0123456789abcdef" for c in workspace_id):
            raise ValueError("Invalid workspace ID")
        path = self.state_dir / "locks" / (workspace_id + ".lock")
        fd = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            deadline = asyncio.get_running_loop().time() + 30
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if asyncio.get_running_loop().time() >= deadline:
                        raise ValueError("This conversation has an active foreground operation; retry when it finishes")
                    await asyncio.sleep(0.05)
            yield
        finally:
            os.close(fd)

    def executor(self, host: str, cwd: str, project_root: str | None = None, *, remote_state_dir: str | None = None):
        # No default-host fallback and no inherited activation/preamble.
        if host not in self.config.hosts:
            raise ValueError("Host is not in the configured allowlist")
        key = (host, cwd, project_root, remote_state_dir)
        if key not in self._executors:
            config = replace(self.config.hosts[host], default_cwd=cwd, preamble="")
            self._executors[key] = self.executor_factory(
                config, remote_state_dir=remote_state_dir)
        return self._executors[key]

    def snapshot(self, state: WorkspaceState, target: WorkspaceTarget | None = None) -> ExecutionSnapshot:
        if target is None:
            if state.host is None:
                raise WorkspaceUnbound("This conversation has no remote workspace. Use workspace_switch(host, cwd) first")
            target = WorkspaceTarget(state.target_id, state.workspace_id, state.host,
                                     state.source_cwd, state.cwd, state.project_root)
        if target.workspace_id != state.workspace_id or not all((target.cwd, target.project_root, target.target_id)):
            raise ValueError("Invalid workspace target")
        from remote_workbench.remote_directory import reject_managed_copy
        reject_managed_copy(target.cwd)
        runtime = self.registry.direct_state_dir(target.target_id)
        if not runtime:
            raise ValueError("Unsupported saved target. Select the actual directory with workspace_switch(host, cwd); project copies are no longer supported")
        executor = self.executor(target.host, target.cwd, target.project_root, remote_state_dir=runtime)
        workspace = Workspace(state.workspace_id, target.host, target.cwd, executor)
        return ExecutionSnapshot(state.thread_id, state.workspace_id, target.host, target.cwd,
                                 target.source_cwd, target.project_root, target.target_id,
                                 state.revision, workspace)

    async def inspect_directory(self, host: str, cwd: str) -> dict:
        from remote_workbench.remote_directory import inspect_directory
        return await inspect_directory(self.executor(host, "/"), cwd)

    async def validate_directory(self, host: str, cwd: str) -> str:
        return (await self.inspect_directory(host, cwd))["cwd"]

    async def switch(self, state: WorkspaceState, host: str, cwd: str, expected_revision: int | None = None):
        """Caller holds the workspace lease; bind the actual directory after validation."""
        expected = state.revision if expected_revision is None else expected_revision
        if isinstance(expected, bool) or not isinstance(expected, int) or expected != state.revision:
            raise RevisionConflict("Workspace revision changed; inspect workspace_info before retrying")
        detail = await self.inspect_directory(host, cwd)
        canonical = detail["cwd"]
        target = self.registry.find_target(state.workspace_id, host, canonical)
        if target is None:
            home = detail.get("home")
            if not isinstance(home, str) or not home.startswith("/"):
                raise ValueError("Remote home directory unavailable for runtime state")
            target_id = uuid.uuid4().hex
            target = WorkspaceTarget(target_id, state.workspace_id, host, canonical, canonical, canonical)
            state_dir = posixpath.join(home, ".local/state/remote-workbench/targets", target_id)
        else:
            state_dir = self.registry.direct_state_dir(target.target_id)
        return self.registry.commit_switch(state.workspace_id, expected, target, remote_state_dir=state_dir)

    def use_local(self, state: WorkspaceState, expected_revision: int | None = None):
        """Clear the active remote binding without contacting any host.

        The caller holds the workspace lease. The registry performs a second
        revision check in the same transaction that clears the binding.
        """
        expected = state.revision if expected_revision is None else expected_revision
        if isinstance(expected, bool) or not isinstance(expected, int) or expected != state.revision:
            raise RevisionConflict("Workspace revision changed; inspect workspace_info before retrying")
        return self.registry.clear_binding(state.workspace_id, expected)
