"""Persistent local identities and target history for session workspaces."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import os
from pathlib import Path
import sqlite3
import json
import posixpath
import re
import time
from typing import Iterator
import uuid


@dataclass(frozen=True, slots=True)
class WorkspaceState:
    workspace_id: str
    thread_id: str
    host: str | None = None
    source_cwd: str | None = None
    cwd: str | None = None
    project_root: str | None = None
    target_id: str | None = None
    revision: int = 0


@dataclass(frozen=True, slots=True)
class WorkspaceTarget:
    target_id: str
    workspace_id: str
    host: str
    source_cwd: str
    cwd: str | None
    project_root: str | None


@dataclass(frozen=True, slots=True)
class JobOperation:
    """Persistent idempotency mapping for a requested background job."""

    workspace_id: str
    idempotency_key: str
    job_id: str
    command_fingerprint: str
    state: str
    target: WorkspaceTarget
    created_at: str
    updated_at: str
    origin_nonce: str | None = None
    launch_cwd: str | None = None
    remote_state_dir: str | None = None
    actual_cwd: str | None = None
    ssh_target: str | None = None
    test_attempt_id: str | None = None


class RevisionConflict(ValueError):
    """Raised when a workspace was changed since the caller read it."""


class Registry:
    """SQLite registry with per-operation connections and transactional CAS."""

    _BUSY_TIMEOUT_MS = 5000
    _MAX_HISTORY_LIMIT = 100

    def __init__(self, path: Path):
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.path.is_symlink():
            raise ValueError("Registry database path must not be a symlink")

        # Create the database with private permissions before SQLite opens it.
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            os.chmod(self.path, 0o600)
        else:
            os.close(fd)

        with self._connection() as db:
            db.execute("PRAGMA journal_mode = WAL")
            db.execute("BEGIN IMMEDIATE")
            try:
                db.execute(
                    """CREATE TABLE IF NOT EXISTS workspaces (
                           workspace_id TEXT PRIMARY KEY,
                           thread_id TEXT NOT NULL UNIQUE,
                           host TEXT,
                           source_cwd TEXT,
                           cwd TEXT,
                           project_root TEXT,
                           target_id TEXT,
                           revision INTEGER NOT NULL DEFAULT 0,
                           created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                           updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                       )"""
                )
                db.execute(
                    """CREATE TABLE IF NOT EXISTS targets (
                           target_id TEXT PRIMARY KEY,
                           workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id)
                               ON DELETE CASCADE,
                           host TEXT NOT NULL,
                           source_cwd TEXT NOT NULL,
                           cwd TEXT,
                           project_root TEXT,
                           UNIQUE(workspace_id, host, source_cwd)
                       )"""
                )
                db.execute(
                    """CREATE TABLE IF NOT EXISTS target_history (
                           history_id INTEGER PRIMARY KEY AUTOINCREMENT,
                           workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id)
                               ON DELETE CASCADE,
                           target_id TEXT NOT NULL,
                           host TEXT NOT NULL,
                           source_cwd TEXT NOT NULL,
                           cwd TEXT,
                           project_root TEXT,
                           revision INTEGER NOT NULL,
                           changed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                           UNIQUE(workspace_id, revision)
                       )"""
                )
                db.execute(
                    """CREATE TABLE IF NOT EXISTS jobs (
                           workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id)
                               ON DELETE CASCADE,
                           job_id TEXT NOT NULL,
                           target_id TEXT NOT NULL,
                           host TEXT NOT NULL,
                           source_cwd TEXT NOT NULL,
                           cwd TEXT,
                           project_root TEXT,
                           origin_nonce TEXT,
                           launch_cwd TEXT,
                           remote_state_dir TEXT,
                           actual_cwd TEXT,
                           ssh_target TEXT,
                           PRIMARY KEY(workspace_id, job_id)
                       )"""
                )
                db.execute(
                    """CREATE TABLE IF NOT EXISTS job_operations (
                           workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id)
                               ON DELETE CASCADE,
                           idempotency_key TEXT NOT NULL,
                           job_id TEXT NOT NULL,
                           command_fingerprint TEXT NOT NULL,
                           target_id TEXT NOT NULL,
                           host TEXT NOT NULL,
                           source_cwd TEXT NOT NULL,
                           cwd TEXT,
                           project_root TEXT,
                           origin_nonce TEXT,
                           launch_cwd TEXT,
                           remote_state_dir TEXT,
                           actual_cwd TEXT,
                           ssh_target TEXT,
                           state TEXT NOT NULL,
                           created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                           updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                           PRIMARY KEY(workspace_id, idempotency_key),
                           UNIQUE(workspace_id, job_id)
                       )"""
                )
                # These nullable fields are an additive compatibility layer.
                # Existing job origins remain readable, but without a nonce
                # they cannot be safely recovered after a lost launch reply.
                for table in ("jobs", "job_operations"):
                    columns = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
                    for name in ("origin_nonce", "launch_cwd", "remote_state_dir", "actual_cwd",
                                 "ssh_target", "test_attempt_id"):
                        if name not in columns:
                            db.execute(f"ALTER TABLE {table} ADD COLUMN {name} TEXT")
                db.execute(
                    "CREATE INDEX IF NOT EXISTS target_history_recent "
                    "ON target_history(workspace_id, revision DESC)"
                )
                db.execute("""CREATE TABLE IF NOT EXISTS call_metrics (
                    id INTEGER PRIMARY KEY, workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id),
                    tool TEXT NOT NULL, argument_bytes INTEGER NOT NULL, result_bytes INTEGER NOT NULL,
                    duration_ms INTEGER NOT NULL, failed INTEGER NOT NULL,
                    read_key TEXT, repeated_read INTEGER NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )""")
                db.execute("CREATE INDEX IF NOT EXISTS call_metrics_workspace ON call_metrics(workspace_id)")
                db.execute("CREATE INDEX IF NOT EXISTS call_metrics_reads ON call_metrics(workspace_id, read_key)")
                db.execute("""CREATE TABLE IF NOT EXISTS last_tests (
                    workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id), target_id TEXT NOT NULL,
                    observation TEXT NOT NULL, attempt_order INTEGER, attempt_id TEXT,
                    PRIMARY KEY(workspace_id, target_id)
                )""")
                last_test_columns = {row[1] for row in db.execute("PRAGMA table_info(last_tests)")}
                for name, declaration in (("attempt_order", "INTEGER"), ("attempt_id", "TEXT")):
                    if name not in last_test_columns:
                        db.execute(f"ALTER TABLE last_tests ADD COLUMN {name} {declaration}")
                db.execute("""CREATE TABLE IF NOT EXISTS test_attempts (
                    attempt_order INTEGER PRIMARY KEY AUTOINCREMENT,
                    attempt_id TEXT NOT NULL UNIQUE,
                    workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id) ON DELETE CASCADE,
                    target_id TEXT NOT NULL,
                    job_id TEXT NOT NULL,
                    execution_kind TEXT NOT NULL,
                    target_json TEXT NOT NULL,
                    workspace_revision INTEGER NOT NULL DEFAULT 0,
                    command TEXT NOT NULL,
                    cwd TEXT NOT NULL,
                    started_at REAL NOT NULL,
                    state TEXT NOT NULL,
                    process_exit_status TEXT NOT NULL,
                    exit_code INTEGER,
                    test_result TEXT NOT NULL,
                    before_json TEXT,
                    after_json TEXT,
                    summary_json TEXT,
                    observation_json TEXT,
                    test_result_reason TEXT,
                    output_id TEXT,
                    finished_at REAL,
                    error TEXT,
                    UNIQUE(workspace_id, job_id)
                )""")
                attempt_columns = {row[1] for row in db.execute("PRAGMA table_info(test_attempts)")}
                for name, declaration in (("workspace_revision", "INTEGER NOT NULL DEFAULT 0"),
                                          ("observation_json", "TEXT"),
                                          ("test_result_reason", "TEXT")):
                    if name not in attempt_columns:
                        db.execute(f"ALTER TABLE test_attempts ADD COLUMN {name} {declaration}")
                db.execute("""CREATE INDEX IF NOT EXISTS test_attempts_target_recent
                    ON test_attempts(workspace_id, target_id, attempt_order DESC)""")
                db.execute("""CREATE TABLE IF NOT EXISTS workspace_notes (
                    workspace_id TEXT PRIMARY KEY REFERENCES workspaces(workspace_id) ON DELETE CASCADE,
                    note TEXT NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )""")
                db.commit()
            except BaseException:
                db.rollback()
                raise

        # SQLite creates sidecar files from the database's mode. Tighten any
        # that already exist as well, including files left by another process.
        for private_file in (self.path, Path(str(self.path) + "-wal"),
                             Path(str(self.path) + "-shm")):
            if private_file.exists():
                os.chmod(private_file, 0o600)

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(
            self.path,
            timeout=self._BUSY_TIMEOUT_MS / 1000,
            isolation_level=None,
        )
        db.row_factory = sqlite3.Row
        db.execute(f"PRAGMA busy_timeout = {self._BUSY_TIMEOUT_MS}")
        db.execute("PRAGMA foreign_keys = ON")
        try:
            yield db
        finally:
            db.close()

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                yield db
                db.commit()
            except BaseException:
                db.rollback()
                raise

    @staticmethod
    def _state(row: sqlite3.Row) -> WorkspaceState:
        return WorkspaceState(
            workspace_id=row["workspace_id"],
            thread_id=row["thread_id"],
            host=row["host"],
            source_cwd=row["source_cwd"],
            cwd=row["cwd"],
            project_root=row["project_root"],
            target_id=row["target_id"],
            revision=row["revision"],
        )

    @staticmethod
    def _target(row: sqlite3.Row) -> WorkspaceTarget:
        return WorkspaceTarget(
            target_id=row["target_id"],
            workspace_id=row["workspace_id"],
            host=row["host"],
            source_cwd=row["source_cwd"],
            cwd=row["cwd"],
            project_root=row["project_root"],
        )

    @staticmethod
    def _validate_target(workspace_id: str, target: WorkspaceTarget) -> None:
        if target.workspace_id != workspace_id:
            raise ValueError("Target belongs to a different workspace")
        for name in ("target_id", "host", "source_cwd"):
            value = getattr(target, name)
            if not isinstance(value, str) or not value:
                raise ValueError(f"Target {name} must be a non-empty string")
        for name in ("cwd", "project_root"):
            value = getattr(target, name)
            if value is not None and not isinstance(value, str):
                raise ValueError(f"Target {name} must be a string or None")

    def get_or_create(self, thread_id: str) -> WorkspaceState:
        if not isinstance(thread_id, str) or not thread_id:
            raise ValueError("thread_id must be a non-empty string")
        with self._transaction() as db:
            db.execute(
                "INSERT INTO workspaces(workspace_id, thread_id) VALUES (?, ?) "
                "ON CONFLICT(thread_id) DO NOTHING",
                (uuid.uuid4().hex, thread_id),
            )
            row = db.execute(
                "SELECT * FROM workspaces WHERE thread_id = ?", (thread_id,)
            ).fetchone()
            return self._state(row)

    def get(self, workspace_id: str) -> WorkspaceState:
        with self._connection() as db:
            row = db.execute(
                "SELECT * FROM workspaces WHERE workspace_id = ?", (workspace_id,)
            ).fetchone()
        if row is None:
            raise ValueError(f"Unknown workspace: {workspace_id}")
        return self._state(row)

    def find_target(
        self, workspace_id: str, host: str, source_cwd: str
    ) -> WorkspaceTarget | None:
        with self._connection() as db:
            row = db.execute(
                """SELECT * FROM targets
                   WHERE workspace_id = ? AND host = ? AND source_cwd = ?""",
                (workspace_id, host, source_cwd),
            ).fetchone()
        return self._target(row) if row is not None else None

    def commit_switch(
        self,
        workspace_id: str,
        expected_revision: int,
        target: WorkspaceTarget,
    ) -> WorkspaceState:
        self._validate_target(workspace_id, target)
        with self._transaction() as db:
            row = db.execute(
                "SELECT * FROM workspaces WHERE workspace_id = ?", (workspace_id,)
            ).fetchone()
            if row is None:
                raise ValueError(f"Unknown workspace: {workspace_id}")
            current = self._state(row)
            if current.revision != expected_revision:
                raise RevisionConflict(
                    f"Workspace {workspace_id} revision is {current.revision}, "
                    f"expected {expected_revision}"
                )

            existing = db.execute(
                """SELECT * FROM targets
                   WHERE workspace_id = ? AND host = ? AND source_cwd = ?""",
                (workspace_id, target.host, target.source_cwd),
            ).fetchone()
            canonical_target_id = existing["target_id"] if existing else target.target_id
            if existing is not None and self._target(existing) != target:
                raise ValueError("An existing target is immutable; resume its saved project directory")

            # Re-selecting the exact current target is an idempotent no-op:
            # it neither creates a history row nor increments the revision.
            if (
                current.target_id == canonical_target_id
                and current.host == target.host
                and current.source_cwd == target.source_cwd
                and current.cwd == target.cwd
                and current.project_root == target.project_root
            ):
                return current

            if existing is None:
                try:
                    db.execute(
                        """INSERT INTO targets
                           (target_id, workspace_id, host, source_cwd, cwd, project_root)
                           VALUES (?, ?, ?, ?, ?, ?)""",
                        (target.target_id, workspace_id, target.host, target.source_cwd,
                         target.cwd, target.project_root),
                    )
                except sqlite3.IntegrityError as exc:
                    raise ValueError("Target ID is already in use") from exc

            revision = current.revision + 1
            db.execute(
                """INSERT INTO target_history
                   (workspace_id, target_id, host, source_cwd, cwd, project_root, revision)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (workspace_id, canonical_target_id, target.host, target.source_cwd,
                 target.cwd, target.project_root, revision),
            )
            db.execute(
                """UPDATE workspaces
                   SET host = ?, source_cwd = ?, cwd = ?, project_root = ?,
                       target_id = ?, revision = ?, updated_at = CURRENT_TIMESTAMP
                   WHERE workspace_id = ?""",
                (target.host, target.source_cwd, target.cwd, target.project_root,
                 canonical_target_id, revision, workspace_id),
            )
            updated = db.execute(
                "SELECT * FROM workspaces WHERE workspace_id = ?", (workspace_id,)
            ).fetchone()
            return self._state(updated)

    def clear_binding(self, workspace_id: str, expected_revision: int) -> WorkspaceState:
        """Atomically return a thread to its native-local default.

        Only the active binding is cleared. Immutable targets, target history,
        and job origins remain available for later resume and job control.
        """
        if isinstance(expected_revision, bool) or not isinstance(expected_revision, int):
            raise ValueError("expected_revision must be an integer")
        with self._transaction() as db:
            row = db.execute(
                "SELECT * FROM workspaces WHERE workspace_id = ?", (workspace_id,)
            ).fetchone()
            if row is None:
                raise ValueError(f"Unknown workspace: {workspace_id}")
            current = self._state(row)
            if current.revision != expected_revision:
                raise RevisionConflict(
                    f"Workspace {workspace_id} revision is {current.revision}, "
                    f"expected {expected_revision}"
                )
            if current.host is None:
                return current
            db.execute(
                """UPDATE workspaces
                   SET host = NULL, source_cwd = NULL, cwd = NULL,
                       project_root = NULL, target_id = NULL,
                       revision = revision + 1, updated_at = CURRENT_TIMESTAMP
                   WHERE workspace_id = ?""",
                (workspace_id,),
            )
            updated = db.execute(
                "SELECT * FROM workspaces WHERE workspace_id = ?", (workspace_id,)
            ).fetchone()
            return self._state(updated)

    def history(self, workspace_id: str, limit: int = 20) -> list[dict]:
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 0:
            raise ValueError("limit must be a non-negative integer")
        bounded_limit = min(limit, self._MAX_HISTORY_LIMIT)
        with self._connection() as db:
            rows = db.execute(
                """SELECT target_id, workspace_id, host, source_cwd, cwd,
                          project_root, revision, changed_at
                   FROM target_history WHERE workspace_id = ?
                   ORDER BY revision DESC LIMIT ?""",
                (workspace_id, bounded_limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def register_job(
        self, workspace_id: str, job_id: str, target: WorkspaceTarget, *,
        origin_nonce: str | None = None, launch_cwd: str | None = None,
        remote_state_dir: str | None = None, ssh_target: str | None = None,
    ) -> None:
        self._validate_target(workspace_id, target)
        self._validate_job_origin(origin_nonce, launch_cwd, remote_state_dir)
        if ssh_target is not None and (not isinstance(ssh_target, str) or not ssh_target):
            raise ValueError("ssh_target must be a non-empty string or None")
        if not isinstance(job_id, str) or not job_id:
            raise ValueError("job_id must be a non-empty string")
        with self._transaction() as db:
            workspace = db.execute(
                "SELECT 1 FROM workspaces WHERE workspace_id = ?", (workspace_id,)
            ).fetchone()
            if workspace is None:
                raise ValueError(f"Unknown workspace: {workspace_id}")
            existing = db.execute(
                "SELECT * FROM jobs WHERE workspace_id = ? AND job_id = ?",
                (workspace_id, job_id),
            ).fetchone()
            snapshot = (
                target.target_id, target.host, target.source_cwd,
                target.cwd, target.project_root,
            )
            if existing is not None:
                saved = tuple(existing[key] for key in (
                    "target_id", "host", "source_cwd", "cwd", "project_root"
                ))
                if saved != snapshot:
                    raise ValueError("Job is already registered with a different target")
                return
            db.execute(
                """INSERT INTO jobs
                   (workspace_id, job_id, target_id, host, source_cwd, cwd, project_root,
                    origin_nonce, launch_cwd, remote_state_dir, ssh_target)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (workspace_id, job_id, *snapshot, origin_nonce, launch_cwd, remote_state_dir, ssh_target),
            )

    def get_job(self, workspace_id: str, job_id: str) -> WorkspaceTarget:
        with self._connection() as db:
            row = db.execute(
                "SELECT * FROM jobs WHERE workspace_id = ? AND job_id = ?",
                (workspace_id, job_id),
            ).fetchone()
        if row is None:
            raise ValueError(f"Unknown job for workspace: {job_id}")
        return WorkspaceTarget(
            target_id=row["target_id"],
            workspace_id=row["workspace_id"],
            host=row["host"],
            source_cwd=row["source_cwd"],
            cwd=row["cwd"],
            project_root=row["project_root"],
        )

    @staticmethod
    def _validate_job_origin(origin_nonce, launch_cwd, remote_state_dir):
        if origin_nonce is not None and (
                not isinstance(origin_nonce, str) or
                not re.fullmatch(r"[a-f0-9]{32}", origin_nonce)):
            raise ValueError("origin_nonce must be 32 lowercase hexadecimal characters")
        for name, value in (("launch_cwd", launch_cwd), ("remote_state_dir", remote_state_dir)):
            if value is not None and (not isinstance(value, str) or not posixpath.isabs(value) or "\x00" in value):
                raise ValueError(f"{name} must be an absolute remote path or None")

    def get_job_origin(self, workspace_id: str, job_id: str) -> dict:
        """Read the immutable target and launch identity saved before submission."""
        with self._connection() as db:
            row = db.execute(
                "SELECT * FROM jobs WHERE workspace_id = ? AND job_id = ?",
                (workspace_id, job_id),
            ).fetchone()
        if row is None:
            raise ValueError(f"Unknown job for workspace: {job_id}")
        return {
            "workspace_id": row["workspace_id"],
            "job_id": row["job_id"],
            "target": WorkspaceTarget(
                target_id=row["target_id"], workspace_id=row["workspace_id"],
                host=row["host"], source_cwd=row["source_cwd"],
                cwd=row["cwd"], project_root=row["project_root"],
            ),
            "origin_nonce": row["origin_nonce"],
            "launch_cwd": row["launch_cwd"],
            "remote_state_dir": row["remote_state_dir"],
            "actual_cwd": row["actual_cwd"],
            "ssh_target": row["ssh_target"],
            "test_attempt_id": row["test_attempt_id"],
        }

    def list_jobs(self, workspace_id: str, limit: int | None = None, offset: int = 0) -> list[dict]:
        """List job handles in newest-registration order with optional pagination.

        The no-argument form remains compatible and returns all jobs for the
        workspace in job-id order. Paginated requests use newest registration
        first; rowid preserves insertion order without a schema migration.
        Every query remains workspace-scoped.
        """
        if limit is not None and (isinstance(limit, bool) or not isinstance(limit, int) or limit < 0):
            raise ValueError("limit must be a non-negative integer or None")
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise ValueError("offset must be a non-negative integer")
        ordering = "rowid DESC" if limit is not None or offset else "job_id"
        query = f"""SELECT job_id, target_id, workspace_id, host, source_cwd, cwd, project_root
                    FROM jobs WHERE workspace_id = ? ORDER BY {ordering}"""
        params: tuple = (workspace_id,)
        if limit is None:
            if offset:
                query += " LIMIT -1 OFFSET ?"
                params += (offset,)
        else:
            query += " LIMIT ? OFFSET ?"
            params += (limit, offset)
        with self._connection() as db:
            rows = db.execute(query, params).fetchall()
        return [{
            "job_id": row["job_id"],
            "target": WorkspaceTarget(
                target_id=row["target_id"], workspace_id=row["workspace_id"],
                host=row["host"], source_cwd=row["source_cwd"],
                cwd=row["cwd"], project_root=row["project_root"],
            ),
        } for row in rows]

    @staticmethod
    def _job_operation(row: sqlite3.Row) -> JobOperation:
        return JobOperation(
            workspace_id=row["workspace_id"],
            idempotency_key=row["idempotency_key"],
            job_id=row["job_id"],
            command_fingerprint=row["command_fingerprint"],
            state=row["state"],
            target=WorkspaceTarget(
                target_id=row["target_id"], workspace_id=row["workspace_id"],
                host=row["host"], source_cwd=row["source_cwd"],
                cwd=row["cwd"], project_root=row["project_root"],
            ),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            origin_nonce=row["origin_nonce"],
            launch_cwd=row["launch_cwd"],
            remote_state_dir=row["remote_state_dir"],
            actual_cwd=row["actual_cwd"],
            ssh_target=row["ssh_target"],
            test_attempt_id=row["test_attempt_id"],
        )

    def reserve_job_operation(
        self, workspace_id: str, idempotency_key: str, target: WorkspaceTarget,
        job_id: str, command_fingerprint: str, *, origin_nonce: str | None = None,
        launch_cwd: str | None = None, remote_state_dir: str | None = None,
        ssh_target: str | None = None,
    ) -> tuple[JobOperation, bool]:
        """Atomically bind a key to one target/job before any remote submission.

        The returned boolean is true only for the first reservation. Existing
        keys with different inputs fail closed; callers must never submit those
        requests again under a conflicting key.
        """
        self._validate_target(workspace_id, target)
        self._validate_job_origin(origin_nonce, launch_cwd, remote_state_dir)
        if ssh_target is not None and (not isinstance(ssh_target, str) or not ssh_target):
            raise ValueError("ssh_target must be a non-empty string or None")
        if not isinstance(idempotency_key, str) or not idempotency_key:
            raise ValueError("idempotency_key must be non-empty text")
        if not isinstance(job_id, str) or not job_id:
            raise ValueError("job_id must be non-empty text")
        if not isinstance(command_fingerprint, str) or not command_fingerprint:
            raise ValueError("command_fingerprint must be non-empty text")
        with self._transaction() as db:
            workspace = db.execute(
                "SELECT 1 FROM workspaces WHERE workspace_id = ?", (workspace_id,)
            ).fetchone()
            if workspace is None:
                raise ValueError(f"Unknown workspace: {workspace_id}")
            existing = db.execute(
                "SELECT * FROM job_operations WHERE workspace_id = ? AND idempotency_key = ?",
                (workspace_id, idempotency_key),
            ).fetchone()
            target_fields = (target.target_id, target.host, target.source_cwd,
                             target.cwd, target.project_root)
            if existing is not None:
                saved_target = tuple(existing[name] for name in (
                    "target_id", "host", "source_cwd", "cwd", "project_root"
                ))
                if (saved_target != target_fields or
                        existing["command_fingerprint"] != command_fingerprint):
                    raise ValueError("idempotency_key is already bound to different job parameters")
                for name, requested in (("launch_cwd", launch_cwd),
                                        ("remote_state_dir", remote_state_dir),
                                        ("ssh_target", ssh_target)):
                    saved = existing[name]
                    if saved is not None and requested is not None and saved != requested:
                        raise ValueError("idempotency_key is already bound to different job parameters")
                return self._job_operation(existing), False

            collision = db.execute(
                "SELECT 1 FROM jobs WHERE workspace_id = ? AND job_id = ?",
                (workspace_id, job_id),
            ).fetchone()
            if collision is not None:
                raise ValueError("job_id is already registered; use a new idempotency_key")
            db.execute(
                """INSERT INTO jobs
                   (workspace_id, job_id, target_id, host, source_cwd, cwd, project_root,
                    origin_nonce, launch_cwd, remote_state_dir, ssh_target)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (workspace_id, job_id, *target_fields, origin_nonce, launch_cwd, remote_state_dir, ssh_target),
            )
            db.execute(
                """INSERT INTO job_operations
                   (workspace_id, idempotency_key, job_id, command_fingerprint,
                    target_id, host, source_cwd, cwd, project_root, origin_nonce,
                    launch_cwd, remote_state_dir, ssh_target, state)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'reserved')""",
                (workspace_id, idempotency_key, job_id, command_fingerprint, *target_fields,
                 origin_nonce, launch_cwd, remote_state_dir, ssh_target),
            )
            row = db.execute(
                "SELECT * FROM job_operations WHERE workspace_id = ? AND idempotency_key = ?",
                (workspace_id, idempotency_key),
            ).fetchone()
            return self._job_operation(row), True

    def get_job_operation(self, workspace_id: str, idempotency_key: str) -> JobOperation:
        with self._connection() as db:
            row = db.execute(
                "SELECT * FROM job_operations WHERE workspace_id = ? AND idempotency_key = ?",
                (workspace_id, idempotency_key),
            ).fetchone()
        if row is None:
            raise ValueError("Unknown idempotency_key for this workspace")
        return self._job_operation(row)

    def update_job_operation(self, workspace_id: str, idempotency_key: str, state: str) -> JobOperation:
        if state not in {"reserved", "submitted", "rejected", "unknown"}:
            raise ValueError("Invalid job operation state")
        with self._transaction() as db:
            cursor = db.execute(
                """UPDATE job_operations SET state = ?, updated_at = CURRENT_TIMESTAMP
                   WHERE workspace_id = ? AND idempotency_key = ?""",
                (state, workspace_id, idempotency_key),
            )
            if cursor.rowcount != 1:
                raise ValueError("Unknown idempotency_key for this workspace")
            row = db.execute(
                "SELECT * FROM job_operations WHERE workspace_id = ? AND idempotency_key = ?",
                (workspace_id, idempotency_key),
            ).fetchone()
            return self._job_operation(row)

    def update_job_actual_cwd(self, workspace_id: str, job_id: str, actual_cwd: str) -> None:
        """Persist the remote physical cwd only after origin identity was verified."""
        if not isinstance(actual_cwd, str) or not posixpath.isabs(actual_cwd) or "\x00" in actual_cwd:
            raise ValueError("actual_cwd must be an absolute remote path")
        with self._transaction() as db:
            row = db.execute(
                "SELECT actual_cwd FROM jobs WHERE workspace_id = ? AND job_id = ?",
                (workspace_id, job_id),
            ).fetchone()
            if row is None:
                raise ValueError("Unknown job for workspace")
            if row["actual_cwd"] not in (None, actual_cwd):
                raise ValueError("Verified job actual_cwd conflicts with the saved origin")
            db.execute(
                "UPDATE jobs SET actual_cwd = COALESCE(actual_cwd, ?) WHERE workspace_id = ? AND job_id = ?",
                (actual_cwd, workspace_id, job_id),
            )
            db.execute(
                "UPDATE job_operations SET actual_cwd = COALESCE(actual_cwd, ?) WHERE workspace_id = ? AND job_id = ?",
                (actual_cwd, workspace_id, job_id),
            )

    def reopen_rejected_job_operation(
        self, workspace_id: str, idempotency_key: str, *, origin_nonce: str | None = None,
        launch_cwd: str | None = None, remote_state_dir: str | None = None,
        ssh_target: str | None = None,
    ) -> bool:
        """Permit an explicit retry only after a tool confirmed no execution."""
        self._validate_job_origin(origin_nonce, launch_cwd, remote_state_dir)
        if ssh_target is not None and (not isinstance(ssh_target, str) or not ssh_target):
            raise ValueError("ssh_target must be a non-empty string or None")
        with self._transaction() as db:
            endpoint_clause = "ssh_target IS NULL" if ssh_target is None else "ssh_target = ?"
            endpoint_params = () if ssh_target is None else (ssh_target,)
            cursor = db.execute(
                """UPDATE job_operations SET state = 'reserved', origin_nonce = ?,
                          launch_cwd = COALESCE(launch_cwd, ?),
                          remote_state_dir = COALESCE(remote_state_dir, ?),
                          ssh_target = COALESCE(ssh_target, ?),
                          updated_at = CURRENT_TIMESTAMP
                   WHERE workspace_id = ? AND idempotency_key = ? AND state = 'rejected' AND """ + endpoint_clause,
                (origin_nonce, launch_cwd, remote_state_dir, ssh_target,
                 workspace_id, idempotency_key, *endpoint_params),
            )
            if cursor.rowcount == 1:
                row = db.execute(
                    "SELECT job_id FROM job_operations WHERE workspace_id = ? AND idempotency_key = ?",
                    (workspace_id, idempotency_key),
                ).fetchone()
                db.execute(
                    """UPDATE jobs SET origin_nonce = ?, launch_cwd = COALESCE(launch_cwd, ?),
                              remote_state_dir = COALESCE(remote_state_dir, ?),
                              ssh_target = COALESCE(ssh_target, ?)
                       WHERE workspace_id = ? AND job_id = ?""",
                    (origin_nonce, launch_cwd, remote_state_dir, ssh_target,
                     workspace_id, row["job_id"]),
                )
            return cursor.rowcount == 1

    def record_call(self, workspace_id, tool, argument_bytes, result_bytes, duration_ms, failed, read_key=None):
        with self._transaction() as db:
            repeated = bool(read_key) and db.execute(
                "SELECT 1 FROM call_metrics WHERE workspace_id=? AND read_key=? LIMIT 1",
                (workspace_id, read_key)).fetchone() is not None
            db.execute("""INSERT INTO call_metrics
                (workspace_id,tool,argument_bytes,result_bytes,duration_ms,failed,read_key,repeated_read)
                VALUES (?,?,?,?,?,?,?,?)""", (workspace_id, tool, argument_bytes, result_bytes,
                                               duration_ms, int(failed), read_key, int(repeated)))

    def metrics(self, workspace_id):
        with self._connection() as db:
            row = db.execute("""SELECT COUNT(*) AS recorded_responses,
                COALESCE(SUM(argument_bytes),0) AS validated_argument_bytes,
                COALESCE(SUM(result_bytes),0) AS result_text_bytes,
                COALESCE(SUM(failed),0) AS failed_results,
                COALESCE(SUM(repeated_read),0) AS repeated_output_reads,
                COALESCE(SUM(duration_ms),0) AS total_duration_ms
                FROM call_metrics WHERE workspace_id=?""", (workspace_id,)).fetchone()
        return dict(row)

    @staticmethod
    def _test_attempt(row: sqlite3.Row | None) -> dict | None:
        if row is None:
            return None
        value = dict(row)
        for column, key in (("target_json", "target"), ("before_json", "before"),
                            ("after_json", "after"), ("summary_json", "summary"),
                            ("observation_json", "observation")):
            raw = value.pop(column, None)
            value[key] = json.loads(raw) if raw else None
        return value

    def begin_test_attempt(self, workspace_id: str, target: WorkspaceTarget, *,
                           attempt_id: str, job_id: str, command: str, cwd: str,
                           execution_kind: str, started_at: float | None = None,
                           before: dict | None = None, revision: int = 0) -> dict:
        """Persist a test attempt before its remote command can start.

        Job IDs make retries idempotent. AUTOINCREMENT attempt_order preserves
        launch order across processes even when completion events arrive later.
        """
        self._validate_target(workspace_id, target)
        if not isinstance(attempt_id, str) or not re.fullmatch(r"[a-f0-9]{32}", attempt_id):
            raise ValueError("attempt_id must be a 32-character lowercase hexadecimal ID")
        if not isinstance(job_id, str) or not job_id or len(job_id) > 80:
            raise ValueError("test attempt job_id must be non-empty and at most 80 characters")
        if not isinstance(command, str) or not command:
            raise ValueError("test attempt command must be non-empty text")
        if not isinstance(cwd, str) or not posixpath.isabs(cwd) or "\x00" in cwd:
            raise ValueError("test attempt cwd must be an absolute path")
        if execution_kind not in {"foreground", "background"}:
            raise ValueError("execution_kind must be foreground or background")
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
            raise ValueError("revision must be a non-negative integer")
        started_at = time.time() if started_at is None else started_at
        if isinstance(started_at, bool) or not isinstance(started_at, (int, float)):
            raise ValueError("started_at must be a timestamp")
        target_value = {
            "workspace_id": target.workspace_id, "target_id": target.target_id,
            "host": target.host, "source_cwd": target.source_cwd,
            "cwd": target.cwd, "project_root": target.project_root,
        }
        with self._transaction() as db:
            existing = db.execute(
                "SELECT * FROM test_attempts WHERE workspace_id = ? AND job_id = ?",
                (workspace_id, job_id),
            ).fetchone()
            if existing is not None:
                value = self._test_attempt(existing)
                if (value["target_id"] != target.target_id or value["target"] != target_value or
                        value["command"] != command or value["cwd"] != cwd):
                    raise ValueError("Test job is already bound to different attempt parameters")
                return value
            collision = db.execute(
                "SELECT attempt_id FROM test_attempts WHERE attempt_id = ?", (attempt_id,)
            ).fetchone()
            if collision:
                raise ValueError("Test attempt ID is already in use")
            db.execute(
                """INSERT INTO test_attempts
                   (attempt_id, workspace_id, target_id, job_id, execution_kind, target_json,
                    workspace_revision, command, cwd, started_at, state, process_exit_status,
                    test_result, before_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'running', 'unknown', 'unknown', ?)""",
                (attempt_id, workspace_id, target.target_id, job_id, execution_kind,
                 json.dumps(target_value, separators=(",", ":")), revision, command, cwd, started_at,
                 json.dumps(before, separators=(",", ":")) if before is not None else None),
            )
            # Background jobs already have an immutable C2 origin reservation.
            # Add the attempt link in the same transaction as its insertion.
            for table in ("jobs", "job_operations"):
                db.execute(
                    f"UPDATE {table} SET test_attempt_id = ? WHERE workspace_id = ? AND job_id = ?",
                    (attempt_id, workspace_id, job_id),
                )
            row = db.execute("SELECT * FROM test_attempts WHERE attempt_id = ?", (attempt_id,)).fetchone()
            return self._test_attempt(row)

    def update_test_attempt(self, workspace_id: str, attempt_id: str, *, state: str,
                            process_exit_status: str = "unknown", exit_code: int | None = None,
                            test_result: str = "unknown", summary: dict | None = None,
                            test_result_reason: str | None = None,
                            before: dict | None = None, after: dict | None = None,
                            output_id: str | None = None,
                            finished_at: float | None = None, error: str | None = None,
                            observation: dict | None = None,
                            expected_target_id: str | None = None,
                            expected_job_id: str | None = None) -> dict:
        """Persist an attempt event without letting late/unknown events downgrade completion."""
        if state not in {"running", "finished", "failed", "cancelled", "timed_out", "unknown", "rejected"}:
            raise ValueError("Invalid test attempt state")
        if process_exit_status not in {"success", "failed", "cancelled", "timed_out", "unknown", "not_started"}:
            raise ValueError("Invalid process_exit_status")
        if test_result not in {"success", "failed", "completed", "unknown"}:
            raise ValueError("Invalid test_result")
        terminal_states = {"finished", "failed", "cancelled", "timed_out", "rejected"}
        finished_at = time.time() if finished_at is None and state in terminal_states else finished_at
        with self._transaction() as db:
            row = db.execute(
                "SELECT * FROM test_attempts WHERE workspace_id = ? AND attempt_id = ?",
                (workspace_id, attempt_id),
            ).fetchone()
            if row is None:
                raise ValueError("Unknown test attempt")
            if expected_target_id is not None and row["target_id"] != expected_target_id:
                raise ValueError("Test attempt target identity mismatch")
            if expected_job_id is not None and row["job_id"] != expected_job_id:
                raise ValueError("Test attempt job identity mismatch")
            existing = row["state"]
            terminal = {"finished", "failed", "cancelled", "timed_out", "rejected"}
            if existing in terminal:
                return self._test_attempt(row)
            db.execute(
                """UPDATE test_attempts SET state = ?, process_exit_status = ?, exit_code = ?,
                          test_result = ?, summary_json = COALESCE(?, summary_json),
                          observation_json = COALESCE(?, observation_json),
                          test_result_reason = COALESCE(?, test_result_reason),
                          before_json = COALESCE(?, before_json),
                          after_json = COALESCE(?, after_json), output_id = COALESCE(?, output_id),
                          finished_at = CASE WHEN ? IN ('running','unknown') THEN NULL
                                              ELSE COALESCE(?, finished_at) END,
                          error = CASE WHEN ? IN ('running','finished','failed','cancelled','timed_out')
                                       THEN ? ELSE COALESCE(?, error) END
                   WHERE workspace_id = ? AND attempt_id = ?""",
                (state, process_exit_status, exit_code, test_result,
                 json.dumps(summary, separators=(",", ":")) if summary is not None else None,
                 json.dumps(observation, separators=(",", ":")) if observation is not None else None,
                 test_result_reason,
                 json.dumps(before, separators=(",", ":")) if before is not None else None,
                 json.dumps(after, separators=(",", ":")) if after is not None else None,
                 output_id, state, finished_at, state, error, error, workspace_id, attempt_id),
            )
            if observation is not None and observation.get("reliable_completion") is True:
                saved = dict(observation, attempt_id=attempt_id, attempt_order=row["attempt_order"])
                self._save_test_in_transaction(db, workspace_id, row["target_id"], saved,
                                               row["attempt_order"], attempt_id)
            updated = db.execute(
                "SELECT * FROM test_attempts WHERE workspace_id = ? AND attempt_id = ?",
                (workspace_id, attempt_id),
            ).fetchone()
            return self._test_attempt(updated)

    def test_attempt(self, workspace_id: str, attempt_id: str) -> dict | None:
        with self._connection() as db:
            row = db.execute(
                "SELECT * FROM test_attempts WHERE workspace_id = ? AND attempt_id = ?",
                (workspace_id, attempt_id),
            ).fetchone()
        return self._test_attempt(row)

    def test_attempt_for_job(self, workspace_id: str, job_id: str) -> dict | None:
        with self._connection() as db:
            row = db.execute(
                "SELECT * FROM test_attempts WHERE workspace_id = ? AND job_id = ?",
                (workspace_id, job_id),
            ).fetchone()
        return self._test_attempt(row)

    def latest_test_attempt(self, workspace_id: str, target_id: str) -> dict | None:
        with self._connection() as db:
            row = db.execute(
                """SELECT * FROM test_attempts WHERE workspace_id = ? AND target_id = ?
                   ORDER BY attempt_order DESC LIMIT 1""",
                (workspace_id, target_id),
            ).fetchone()
        return self._test_attempt(row)

    def _save_test_in_transaction(self, db, workspace_id, target_id, observation,
                                  attempt_order=None, attempt_id=None):
        existing = db.execute(
            "SELECT attempt_order FROM last_tests WHERE workspace_id = ? AND target_id = ?",
            (workspace_id, target_id),
        ).fetchone()
        if (attempt_order is not None and existing is not None and
                existing["attempt_order"] is not None and existing["attempt_order"] > attempt_order):
            return False
        db.execute(
            """INSERT INTO last_tests(workspace_id, target_id, observation, attempt_order, attempt_id)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(workspace_id, target_id) DO UPDATE SET
                   observation=excluded.observation,
                   attempt_order=excluded.attempt_order,
                   attempt_id=excluded.attempt_id""",
            (workspace_id, target_id, json.dumps(observation, separators=(",", ":")),
             attempt_order, attempt_id),
        )
        return True

    def save_test(self, workspace_id, target_id, observation):
        with self._transaction() as db:
            order = observation.get("attempt_order") if isinstance(observation, dict) else None
            attempt_id = observation.get("attempt_id") if isinstance(observation, dict) else None
            return self._save_test_in_transaction(db, workspace_id, target_id, observation, order, attempt_id)

    def last_test(self, workspace_id, target_id):
        with self._connection() as db:
            row = db.execute("SELECT observation FROM last_tests WHERE workspace_id=? AND target_id=?",
                             (workspace_id, target_id)).fetchone()
        return json.loads(row[0]) if row else None

    def workspace_note(self, workspace_id: str, updates: dict | None = None) -> dict | None:
        """Read or merge a small local recovery note scoped to one workspace."""
        if updates is None:
            with self._connection() as db:
                row = db.execute(
                    "SELECT note, updated_at FROM workspace_notes WHERE workspace_id = ?",
                    (workspace_id,),
                ).fetchone()
            if row is None:
                return None
            return {**json.loads(row["note"]), "updated_at": row["updated_at"]}

        allowed = {"goal", "constraints", "decisions", "next_step"}
        if not isinstance(updates, dict) or set(updates) - allowed:
            raise ValueError("workspace note accepts goal, constraints, decisions and next_step")
        if any(not isinstance(value, str) for value in updates.values()):
            raise ValueError("workspace note values must be text")
        with self._transaction() as db:
            workspace = db.execute(
                "SELECT 1 FROM workspaces WHERE workspace_id = ?", (workspace_id,)
            ).fetchone()
            if workspace is None:
                raise ValueError(f"Unknown workspace: {workspace_id}")
            row = db.execute(
                "SELECT note FROM workspace_notes WHERE workspace_id = ?", (workspace_id,)
            ).fetchone()
            note = json.loads(row["note"]) if row else {}
            note.update(updates)
            db.execute(
                """INSERT INTO workspace_notes(workspace_id, note) VALUES (?, ?)
                   ON CONFLICT(workspace_id) DO UPDATE SET note = excluded.note,
                       updated_at = CURRENT_TIMESTAMP""",
                (workspace_id, json.dumps(note, ensure_ascii=False, separators=(",", ":"))),
            )
            saved = db.execute(
                "SELECT note, updated_at FROM workspace_notes WHERE workspace_id = ?",
                (workspace_id,),
            ).fetchone()
            return {**json.loads(saved["note"]), "updated_at": saved["updated_at"]}
