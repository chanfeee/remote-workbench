"""Session-local records; the default remote workspace is the original directory."""
from __future__ import annotations

import asyncio
from dataclasses import replace
import json
import posixpath
import time
import uuid

from remote_workbench import audit
from remote_workbench.ssh import SSHExecutor


class WorkspaceSession:
    def __init__(self, template):
        self.directory_mode = "direct"
        self.id = uuid.uuid4().hex
        self.source = template
        self.audit_dir = audit.base_root() / "sessions" / self.id
        self.audit_dir.mkdir(mode=0o700, parents=True, exist_ok=False)
        from remote_workbench.resource_claims import ResourceClaims
        self.resources = ResourceClaims(audit.base_root() / 'workspace-resources',
                                         audit_root=audit.base_root() / 'sessions')
        self.project_root = template.cwd
        config = replace(template.executor.config, default_cwd=template.cwd)
        self.workspace = replace(template, executor=SSHExecutor(config))
        self.prepared = False
        self._prepare_lock = asyncio.Lock()
        self._save()

    def _save(self):
        path = self.audit_dir / "session.json"
        value = {"session_id": self.id, "host": self.source.host,
                 "source_cwd": self.source.cwd, "cwd": self.workspace.cwd,
                 "prepared": self.prepared, "directory_mode": self.directory_mode}
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(value, indent=2) + "\n")
        temporary.chmod(0o600)
        temporary.replace(path)

    async def ensure_ready(self):
        if self.prepared:
            return
        async with self._prepare_lock:
            if self.prepared:
                return
            with audit.scoped_root(self.audit_dir):
                record = audit.create(kind="tool", tool="validate_workspace", host=self.source.host,
                                      cwd=self.workspace.cwd, status="running",
                                      arguments={"source_cwd": self.source.cwd, "session_id": self.id})
                token = audit.operation_id.set(record)
                started = time.monotonic()
                try:
                    from remote_workbench.remote_directory import inspect_directory
                    detail = await inspect_directory(self.source.executor, self.source.cwd)
                    home = detail.get('home')
                    if not isinstance(home, str) or not home.startswith('/'):
                        raise ValueError('Remote home directory unavailable for runtime state')
                    cwd = detail['cwd']
                    self.project_root = cwd
                    config = replace(self.workspace.executor.config, default_cwd=cwd)
                    self.workspace = replace(self.workspace, cwd=cwd, executor=SSHExecutor(config,
                        remote_state_dir=posixpath.join(home, '.local/state/remote-workbench/sessions', self.id)))
                    self.prepared = True
                    self._save()
                    audit.update(record, status='finished', cwd=cwd, directory_mode='direct',
                                 elapsed_ms=round((time.monotonic()-started)*1000))
                    return
                except BaseException as exc:
                    audit.update(record, status="failed", error=str(exc))
                    raise
                finally:
                    audit.operation_id.reset(token)
