"""Local cooperative write claims for managed directories, never remote isolation.

Claims survive controller exit. Only an acknowledged terminal output releases an
uncertain claim; a dead local PID alone is not evidence of remote completion.
"""
import json
from pathlib import Path
import posixpath
import sqlite3
import uuid


class ResourceClaims:
    def __init__(self, state_dir, *, audit_root=None):
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.audit_root = Path(audit_root) if audit_root is not None else self.state_dir / 'audit'
        self.path = self.state_dir / "resources.sqlite3"
        with self.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS claims (
                claim_id TEXT PRIMARY KEY, host TEXT NOT NULL, cwd TEXT NOT NULL,
                workspace_id TEXT NOT NULL, tool TEXT NOT NULL, output_id TEXT,
                job_id TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP)""")
        self.path.chmod(0o600)

    def connect(self):
        return sqlite3.connect(self.path, timeout=5)

    def _terminal(self, row):
        if not row["output_id"]:
            return False
        try:
            record = json.loads((self.audit_root / row["workspace_id"] /
                                 row["output_id"] / "record.json").read_text())
        except (OSError, ValueError):
            return False
        # A completed collector is authoritative for a background handle.
        return (record.get("execution_state") == "finished" and
                (not row["job_id"] or record.get("sync_state") == "complete") and
                (record.get("remote_task") or {}).get("cleanup_state") != "unconfirmed")

    def active(self):
        with self.connect() as db:
            db.row_factory = sqlite3.Row
            db.execute("BEGIN IMMEDIATE")
            rows = list(db.execute("SELECT * FROM claims"))
            for row in rows:
                if self._terminal(row):
                    db.execute("DELETE FROM claims WHERE claim_id=?", (row["claim_id"],))
            return [dict(row) for row in db.execute("SELECT * FROM claims")]

    def acquire(self, host, cwd, workspace_id, tool):
        self.active()
        cwd = posixpath.normpath(cwd)
        with self.connect() as db:
            db.row_factory = sqlite3.Row
            db.execute("BEGIN IMMEDIATE")
            for row in db.execute("SELECT * FROM claims WHERE host=?", (host,)):
                if posixpath.commonpath([cwd, row["cwd"]]) in {cwd, row["cwd"]}:
                    raise ValueError("Managed directory busy or completion unknown: " + json.dumps(dict(row)))
            claim = uuid.uuid4().hex
            db.execute("INSERT INTO claims(claim_id,host,cwd,workspace_id,tool) VALUES(?,?,?,?,?)",
                       (claim, host, cwd, workspace_id, tool))
        return claim

    def finish(self, claim, result, *, background=False, shell=False):
        output_id = result.get("job_output_id") if background else result.get("output_id")
        uncertain = result.get("execution_state") in {"unknown", "remote_state_unknown"}
        # Shell completion is only known from the remote acknowledgement.
        command = shell or result.get("execution_state") is not None
        release = (result.get("executed") is False or
                   (not background and not uncertain and
                    (not command or result.get("execution_state") == "finished")))
        with self.connect() as db:
            if release:
                db.execute("DELETE FROM claims WHERE claim_id=?", (claim,))
            else:
                db.execute("UPDATE claims SET output_id=?,job_id=COALESCE(?,job_id) WHERE claim_id=?",
                           (output_id, result.get("job_id") if background else None, claim))

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
