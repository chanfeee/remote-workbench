"""Legacy offline helper for explicitly processing stored background evidence.

The normal job collector no longer imports or calls this module. Its functions
remain available for legacy history tooling and tests only.
"""
from __future__ import annotations

import json
import fcntl
import os
from pathlib import Path
import sqlite3

from remote_workbench import audit, test_evidence
from remote_workbench.workspace_registry import Registry

def _identity(registry: Registry, executor, origin: dict, handle: dict, record: dict):
    workspace_id = origin.get("workspace_id")
    target_id = origin.get("target_id")
    job_id = origin.get("job_id")
    attempt_id = origin.get("test_attempt_id")
    if not all(isinstance(value, str) and value for value in
               (workspace_id, target_id, job_id, attempt_id)):
        return None, "test_attempt_identity_missing"
    attempt = registry.test_attempt(workspace_id, attempt_id)
    if attempt is None:
        return None, "test_attempt_not_found"
    if (attempt.get("execution_kind") != "background" or
            attempt.get("target_id") != target_id or attempt.get("job_id") != job_id or
            handle.get("job_id") != job_id or origin.get("job_id") != handle.get("job_id")):
        return None, "test_attempt_origin_mismatch"
    target = attempt.get("target")
    if not isinstance(target, dict) or target.get("target_id") != target_id or target.get("workspace_id") != workspace_id:
        return None, "test_attempt_target_mismatch"
    if (record.get("workspace_id") not in (None, workspace_id) or
            record.get("target_id") not in (None, target_id) or
            record.get("remote_job_dir") != handle.get("job_dir")):
        return None, "test_attempt_local_record_mismatch"
    if (origin.get("host") != target.get("host") or
            origin.get("target_cwd") != target.get("cwd") or
            origin.get("source_cwd") != target.get("source_cwd") or
            origin.get("project_root") != target.get("project_root") or
            origin.get("launch_cwd") != attempt.get("cwd") or
            origin.get("ssh_target") != executor.config.ssh_target or
            origin.get("remote_state_dir") != executor.remote_state_dir):
        return None, "test_attempt_saved_origin_mismatch"
    return attempt, None


def _complete_output_spools(record_id: str, record: dict) -> bool:
    """Check that complete collector cursors refer to both local output files."""
    try:
        root = audit.directory(record_id)
        remote_bytes = record.get("remote_bytes")
        for stream in ("stdout", "stderr"):
            path = root / stream
            if path.is_symlink() or not path.is_file():
                return False
            size = path.stat().st_size
            saved_size = record.get(stream + "_bytes")
            if isinstance(saved_size, int) and size != saved_size:
                return False
            if isinstance(remote_bytes, dict) and isinstance(remote_bytes.get(stream), int):
                if size != remote_bytes[stream]:
                    return False
        return record.get("sync_state") == "complete"
    except (OSError, ValueError, TypeError):
        return False


async def record_collector_event(executor, handle: dict, record_id: str, *, complete: bool,
                                 error: str | None = None, running_verified: bool = False,
                                 remote_task: dict | None = None) -> dict | None:
    """Serialize event processing for this saved output across MCP processes."""
    try:
        lock_path = audit.directory(record_id) / ".test-evidence.lock"
        lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    except (OSError, ValueError):
        return None
    try:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            # Another collector/resolve call is recording this exact attempt.
            # It owns the retry; do not duplicate fingerprints or writes.
            return None
        return await _record_collector_event_locked(
            executor, handle, record_id, complete=complete, error=error,
            running_verified=running_verified, remote_task=remote_task,
        )
    finally:
        os.close(lock_fd)


async def _record_collector_event_locked(executor, handle: dict, record_id: str, *, complete: bool,
                                         error: str | None = None, running_verified: bool = False,
                                         remote_task: dict | None = None) -> dict | None:
    """Record a collector terminal/disconnect event against the exact saved job."""
    origin = handle.get("origin")
    if not isinstance(origin, dict) or not origin.get("test_attempt_id"):
        return None
    registry_path = origin.get("registry_path")
    if not isinstance(registry_path, str) or not registry_path:
        return None
    try:
        registry = Registry(Path(registry_path))
        record = json.loads((audit.directory(record_id) / "record.json").read_text())
        attempt, reason = _identity(registry, executor, origin, handle, record)
        if reason:
            try:
                audit.update(record_id, test_evidence_state="unknown", test_evidence_error=reason)
            except (OSError, ValueError):
                pass
            return None
        if running_verified:
            saved_task = handle.get("remote_task")
            if not isinstance(remote_task, dict) or not isinstance(saved_task, dict):
                return None
            identity_fields = ("node", "pid", "start_token", "task_id",
                               "task_dir", "origin_nonce", "launch_cwd", "actual_cwd")
            if any(saved_task.get(key) in (None, "") or remote_task.get(key) != saved_task.get(key)
                   for key in identity_fields):
                try:
                    audit.update(record_id, test_evidence_state="unknown",
                                 test_evidence_error="verified_running_identity_mismatch")
                except (OSError, ValueError):
                    pass
                return None
            if (saved_task.get("boot_id") != remote_task.get("boot_id") or
                    (saved_task.get("boot_id") in (None, "") and
                     not str(saved_task.get("start_token", "")).startswith("ps:"))):
                try:
                    audit.update(record_id, test_evidence_state="unknown",
                                 test_evidence_error="verified_running_boot_identity_unavailable")
                except (OSError, ValueError):
                    pass
                return None
            if attempt.get("state") == "running":
                return attempt
            if attempt.get("error"):
                try:
                    # Keep the lost-submission diagnosis in the original local
                    # attempt audit while the durable attempt moves to verified
                    # running and clears it as the current state error.
                    audit.update(attempt["attempt_id"],
                                 recovered_submission_error=attempt["error"],
                                 recovered_output_id=record_id,
                                 test_attempt_state="running")
                except (OSError, ValueError):
                    pass
            updated = registry.update_test_attempt(
                attempt["workspace_id"], attempt["attempt_id"], state="running",
                process_exit_status="unknown", test_result="unknown", error=None,
                output_id=record_id,
                expected_target_id=attempt["target_id"], expected_job_id=attempt["job_id"],
            )
            audit.update(record_id, test_evidence_state="running", test_evidence_error=None)
            return updated
        if attempt.get("state") in {"finished", "failed", "cancelled", "timed_out", "rejected"}:
            if record.get("test_evidence_state") != "complete":
                audit.update(record_id, test_evidence_state="complete", test_evidence_error=None)
            return attempt
        if not complete:
            updated = registry.update_test_attempt(
                attempt["workspace_id"], attempt["attempt_id"], state="unknown",
                process_exit_status="unknown", test_result="unknown", output_id=record_id,
                error=error or "background_log_collection_disconnected",
                expected_target_id=attempt["target_id"], expected_job_id=attempt["job_id"],
            )
            audit.update(record_id, test_evidence_state="unknown",
                         test_evidence_error=error or "background_log_collection_disconnected")
            return updated

        target = attempt["target"]
        after = await test_evidence.capture_target(
            executor, target["host"], target["cwd"], target["project_root"])
        result = {
            "job_id": attempt["job_id"], "output_id": record_id,
            "exit_code": record.get("exit_code"),
            "execution_state": record.get("execution_state", "unknown"),
            "timed_out": bool(record.get("timed_out") or
                               (isinstance(record.get("remote_task"), dict) and
                                record["remote_task"].get("reason") == "timeout")),
            "remote_task": record.get("remote_task"),
            "error": record.get("error"),
        }
        # Only complete, UTF-8, combined stdout+stderr spools up to 1 MiB go
        # through the D1 parser. The streams are parsed separately, never joined.
        summary, summary_stream, summary_reason = test_evidence.summarize_complete_streams(
            result, record_id)
        process_status = test_evidence.process_exit_status(result)
        if result["timed_out"]:
            state = "timed_out"
        elif process_status == "failed":
            state = "failed"
        elif process_status == "success":
            state = "finished"
        elif process_status == "cancelled":
            state = "cancelled"
        else:
            state = "unknown"
        output_complete = _complete_output_spools(record_id, record)
        if not output_complete and state in {"finished", "failed", "cancelled", "timed_out"}:
            # A known remote exit without both verified complete local streams
            # is not a reliable test observation and remains retryable.
            state = "unknown"
            error = error or "complete_output_unavailable"
        test_result = test_evidence.framework_test_result(summary)
        if (attempt.get("error") and
                state in {"finished", "failed", "cancelled", "timed_out"}):
            audit.update(attempt["attempt_id"],
                         recovered_submission_error=attempt["error"],
                         recovered_output_id=record_id,
                         test_attempt_state=state)
        evidence = test_evidence.observation(
            attempt.get("before"), after, result, attempt["command"],
            attempt.get("workspace_revision", 0), attempt=attempt,
            output_id=record_id, summary=summary, summary_stream=summary_stream,
            summary_reason=summary_reason,
            output_complete=output_complete)
        updated = registry.update_test_attempt(
            attempt["workspace_id"], attempt["attempt_id"], state=state,
            process_exit_status=process_status, exit_code=result["exit_code"],
            test_result=test_result, summary=summary,
            test_result_reason=summary_reason,
            after=after, output_id=record_id,
            error=error,
            observation=evidence, expected_target_id=attempt["target_id"],
            expected_job_id=attempt["job_id"],
        )
        if evidence.get("reliable_completion") is True:
            audit.update(record_id, test_evidence_state="complete", test_evidence_error=None)
        else:
            audit.update(record_id, test_evidence_state="unknown",
                         test_evidence_error=error or "verified_process_completion_unavailable")
        return updated
    except (OSError, sqlite3.Error, ValueError, TypeError, KeyError) as exc:
        # The collector keeps its own log state authoritative. Failure to write
        # this extra evidence must never promote an unknown attempt to success.
        try:
            audit.update(record_id, test_evidence_state="unknown",
                         test_evidence_error="attempt_evidence_persist_failed:" + type(exc).__name__)
        except (OSError, ValueError):
            pass
        return None
