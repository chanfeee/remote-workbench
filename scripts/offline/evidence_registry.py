"""Explicit legacy evidence writers; never imported by normal MCP execution."""
from __future__ import annotations

import json
import posixpath
import re
import time

from remote_workbench.workspace_registry import Registry, WorkspaceTarget


class EvidenceRegistry(Registry):
    """Provision historical tables only when an offline tool requests them."""

    def __init__(self, path):
        super().__init__(path)
        with self._transaction() as db:
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
