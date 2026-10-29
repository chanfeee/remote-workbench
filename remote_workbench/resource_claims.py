"""Local cooperative write claims for managed directories, never remote isolation.

Claims survive controller exit. Only an acknowledged terminal output releases an
uncertain claim; a dead local PID alone is not evidence of remote completion.
"""
import json
from contextlib import contextmanager
from contextvars import ContextVar
import fcntl
import os
from pathlib import Path
import posixpath
import sqlite3
import uuid


_current_operation = ContextVar('resource_claim_operation', default=None)


class _Operation:
    def __init__(self, resources, claim):
        self.resources, self.claim = resources, claim
        self.submitted = False
        self.active = True


def mark_dispatch(output_id):
    """Persist intent before SSH submission; never wrap process-creation APIs."""
    operation = _current_operation.get()
    if operation is not None and operation.active:
        operation.resources._dispatch(operation.claim, output_id)
        operation.submitted = True


def mark_job_submission():
    """A background submit API can lose its reply before exposing an output ID."""
    operation = _current_operation.get()
    if operation is not None and operation.active:
        marker = operation.resources._marker(operation.claim)
        if marker is None:
            raise ValueError('Write claim tracking unavailable; no background job was submitted')
        marker['submission_intent'] = True
        operation.resources._write_marker(operation.claim, marker)
        operation.submitted = True


class ResourceClaims:
    def __init__(self, state_dir, *, audit_root=None):
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.audit_root = Path(audit_root) if audit_root is not None else self.state_dir / 'audit'
        self.path = self.state_dir / "resources.sqlite3"
        self.markers = self.state_dir / 'resource-claims'
        self.markers.mkdir(mode=0o700, exist_ok=True)
        self._guards = {}
        with self.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS claims (
                claim_id TEXT PRIMARY KEY, host TEXT NOT NULL, cwd TEXT NOT NULL,
                workspace_id TEXT NOT NULL, tool TEXT NOT NULL, output_id TEXT,
                job_id TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP)""")
        self.path.chmod(0o600)

    def connect(self):
        return sqlite3.connect(self.path, timeout=5)

    def _paths(self, claim):
        if len(claim) != 32 or any(c not in '0123456789abcdef' for c in claim):
            raise ValueError('Invalid claim ID')
        return self.markers / (claim + '.json'), self.markers / (claim + '.lock')

    def _marker(self, claim):
        try:
            path, _ = self._paths(claim)
            value = json.loads(path.read_text())
            outputs = value.get('outputs')
            if (value.get('version') == 1 and isinstance(outputs, list)
                    and all(isinstance(v, str) and len(v) == 32
                            and all(c in '0123456789abcdef' for c in v) for v in outputs)):
                return value
        except (OSError, ValueError, TypeError, AttributeError):
            pass
        return None

    def _write_marker(self, claim, value):
        path, _ = self._paths(claim)
        temporary = path.with_name('.' + uuid.uuid4().hex + '.tmp')
        try:
            with open(temporary, 'x', opener=lambda p, f: os.open(p, f, 0o600)) as output:
                json.dump(value, output, separators=(',', ':'))
                output.flush()
                os.fsync(output.fileno())
            temporary.replace(path)
            fd = os.open(self.markers, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        finally:
            temporary.unlink(missing_ok=True)

    def _close_guard(self, claim):
        fd = self._guards.pop(claim, None)
        if fd is not None:
            os.close(fd)

    def _cleanup(self, claim):
        self._close_guard(claim)
        try:
            for path in self._paths(claim):
                path.unlink(missing_ok=True)
        except (OSError, ValueError):
            pass  # An orphan marker is harmless without a database claim.

    def _owned(self, claim):
        try:
            _, path = self._paths(claim)
            if not path.exists():
                return self._marker(claim) is not None  # Missing guard is not proof of inactivity.
            fd = os.open(path, os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0))
            try:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    return True
                return False
            finally:
                os.close(fd)
        except (OSError, ValueError):
            return True

    def _record(self, row, output):
        try:
            value = json.loads((self.audit_root / row['workspace_id'] / output / 'record.json').read_text())
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError, TypeError):
            return {}

    @staticmethod
    def _acknowledged(record):
        task = record.get('remote_task') or {}
        if not isinstance(task, dict):
            return False
        if task.get('cleanup_state') == 'unconfirmed':
            return False
        return (record.get('execution_state') == 'finished' or
                (record.get('execution_state') == 'cancelled' and task.get('state') == 'finished'))

    def _terminal(self, row):
        if self._owned(row['claim_id']):
            return False
        marker = self._marker(row['claim_id'])
        if (marker is not None and not marker['outputs']
                and not marker.get('uncertain') and not marker.get('submission_intent')):
            return True  # Persisted tracking proves no SSH submission occurred.
        result_output = row['output_id'] or (marker or {}).get('result_output_id')
        if marker is not None and marker.get('uncertain') and not result_output:
            return False  # Explicit unknown outcome without its own completion evidence.
        if row['job_id'] or row['tool'] == 'start_job':
            if not result_output:
                return False  # A completed launcher is not a completed background job.
            record = self._record(row, result_output)
            return self._acknowledged(record) and record.get('sync_state') == 'complete'
        outputs = marker['outputs'] if marker is not None else []
        if result_output and result_output not in outputs:
            outputs = [*outputs, result_output]
        return bool(outputs) and all(self._acknowledged(self._record(row, output)) for output in outputs)

    def _describe(self, row):
        result = dict(row)
        marker = self._marker(row['claim_id'])
        if marker is not None:
            if marker['outputs']:
                result['last_dispatch_output_id'] = marker['outputs'][-1]
            result['recovery_state'] = 'operation_in_progress' if self._owned(row['claim_id']) else 'completion_unconfirmed'
        elif not row['output_id']:
            result['recovery_state'] = 'legacy_claim_without_output'
        return result

    def _dispatch(self, claim, output):
        marker = self._marker(claim)
        if marker is None or claim not in self._guards:
            raise ValueError('Write claim tracking unavailable; no SSH command was submitted')
        if output not in marker['outputs']:
            marker['outputs'].append(output)
            self._write_marker(claim, marker)

    @contextmanager
    def operation(self, claim):
        if claim is None:
            yield None
            return
        operation = _Operation(self, claim)
        token = _current_operation.set(operation)
        try:
            yield operation
        except BaseException:
            # No submit: release. Submitted: retain unless every foreground
            # dispatch has acknowledged completion. Never infer this from a PID.
            operation.active = False
            self._close_guard(claim)
            try:
                removed = False
                with self.connect() as db:
                    db.row_factory = sqlite3.Row
                    row = db.execute('SELECT * FROM claims WHERE claim_id=?', (claim,)).fetchone()
                    if row is not None and (not operation.submitted or self._terminal(row)):
                        db.execute('DELETE FROM claims WHERE claim_id=?', (claim,))
                        removed = True
                if removed:
                    self._cleanup(claim)
            except (OSError, sqlite3.Error):
                pass  # Persisted marker allows the next local inspection to retry.
            raise
        finally:
            operation.active = False
            _current_operation.reset(token)
            self._close_guard(claim)

    def __del__(self):
        for fd in getattr(self, '_guards', {}).values():
            try:
                os.close(fd)
            except OSError:
                pass

    def active(self):
        removed = []
        with self.connect() as db:
            db.row_factory = sqlite3.Row
            db.execute("BEGIN IMMEDIATE")
            rows = list(db.execute("SELECT * FROM claims"))
            for row in rows:
                if self._terminal(row):
                    db.execute("DELETE FROM claims WHERE claim_id=?", (row["claim_id"],))
                    removed.append(row['claim_id'])
            result = [self._describe(row) for row in db.execute("SELECT * FROM claims")]
        for claim in removed:
            self._cleanup(claim)
        return result

    def acquire(self, host, cwd, workspace_id, tool, *, tracked=False):
        self.active()
        cwd = posixpath.normpath(cwd)
        claim = None
        try:
            with self.connect() as db:
                db.row_factory = sqlite3.Row
                db.execute("BEGIN IMMEDIATE")
                for row in db.execute("SELECT * FROM claims WHERE host=?", (host,)):
                    if posixpath.commonpath([cwd, row["cwd"]]) in {cwd, row["cwd"]}:
                        raise ValueError("Managed directory busy or completion unknown: " + json.dumps(self._describe(row)))
                claim = uuid.uuid4().hex
                if tracked:
                    _, path = self._paths(claim)
                    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600)
                    self._guards[claim] = fd
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    self._write_marker(claim, {'version': 1, 'outputs': []})
                db.execute("INSERT INTO claims(claim_id,host,cwd,workspace_id,tool) VALUES(?,?,?,?,?)",
                           (claim, host, cwd, workspace_id, tool))
        except BaseException:
            if claim is not None:
                self._cleanup(claim)
            raise
        return claim

    def finish(self, claim, result, *, background=False, shell=False):
        output_id = result.get("job_output_id") if background else result.get("output_id")
        uncertain = result.get("execution_state") in {"unknown", "remote_state_unknown"}
        # Shell completion is only known from the remote acknowledgement.
        command = shell or result.get("execution_state") is not None
        release = (result.get("executed") is False or
                   (not background and not uncertain and
                    (not command or result.get("execution_state") == "finished")))
        marker = self._marker(claim)
        if marker is not None:
            # A synthetic/unlinked unknown outcome cannot be proved unsubmitted.
            if not release:
                marker['uncertain'] = True
                if isinstance(output_id, str):
                    marker['result_output_id'] = output_id
                self._write_marker(claim, marker)
            if marker['outputs'] and result.get('executed') is not False:
                row = {'workspace_id': self._workspace(claim)}
                unresolved = [output for output in marker['outputs']
                              if not self._acknowledged(self._record(row, output))]
                release = release and not unresolved
                if unresolved and not background:
                    # Old clients only inspect this column. Never publish a
                    # later finished step while an earlier dispatch is unknown.
                    output_id = unresolved[0]
        with self.connect() as db:
            if release:
                db.execute("DELETE FROM claims WHERE claim_id=?", (claim,))
            else:
                db.execute("UPDATE claims SET output_id=?,job_id=COALESCE(?,job_id) WHERE claim_id=?",
                           (output_id, result.get("job_id") if background else None, claim))
        self._close_guard(claim)
        if release:
            self._cleanup(claim)

    def _workspace(self, claim):
        with self.connect() as db:
            row = db.execute('SELECT workspace_id FROM claims WHERE claim_id=?', (claim,)).fetchone()
        return row[0] if row else ''

    def attach_job(self, claim, job_id):
        with self.connect() as db:
            db.execute("UPDATE claims SET job_id=? WHERE claim_id=?", (job_id, claim))

    def reconcile(self, workspace_id, job_id, result):
        if result.get("job_output_id"):
            with self.connect() as db:
                db.execute("UPDATE claims SET output_id=? WHERE workspace_id=? AND job_id=?",
                           (result["job_output_id"], workspace_id, job_id))
        self.active()


def main():
    """Manual local recovery after independently confirming a workload stopped."""
    import argparse
    from remote_workbench.workspace_router import default_state_dir
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=default_state_dir())
    parser.add_argument("--release", metavar="CLAIM_ID")
    parser.add_argument("--confirmed-stopped", action="store_true",
                        help="Operator has independently verified the original remote workload is stopped")
    args = parser.parse_args()
    claims = ResourceClaims(args.data_dir)
    if args.release:
        if not args.confirmed_stopped:
            parser.error("Release requires --confirmed-stopped after verifying the original node/workload; local process exit is insufficient")
        with claims.connect() as db:
            count = db.execute("DELETE FROM claims WHERE claim_id=?", (args.release,)).rowcount
        print(json.dumps({"released": bool(count), "claim_id": args.release}))
    else:
        print(json.dumps({"claims": claims.active()}, ensure_ascii=False))


if __name__ == "__main__":
    main()
