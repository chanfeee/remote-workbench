"""Explicit bounded project fingerprinting; never invoked by ordinary commands."""
from __future__ import annotations
import json
from remote_workbench import audit

_REMOTE = r'''import hashlib

import json

import os

from pathlib import Path

import stat

import sys

TOOL_OWNED = {".git", ".remote-workbench-state", ".remote-workbench-sessions"}

ENVIRONMENT_DIRS = {
    ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache",
    ".mypy_cache", ".ruff_cache", ".tox", ".nox", ".cache",
}

def fail(message, code="invalid_request"):
    print(json.dumps({"error": code, "message": str(message)[:1000]}, separators=(",", ":")))
    raise SystemExit(2)

def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode("utf-8")

def excluded(relative):
    parts = relative.replace(os.sep, "/").split("/")
    return any(part in TOOL_OWNED or part in ENVIRONMENT_DIRS
               or part.startswith(".remote-workbench-write-")
               or part.startswith(".remote-workbench-download-") for part in parts)

def project_fingerprint(project_root, max_files, max_bytes):
    scope = "non-tool-owned files in the selected project; excludes .git and runtime directories; symlinks are hashed without following"
    if (isinstance(max_files, bool) or not isinstance(max_files, int) or max_files < 1
            or isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 1):
        fail("fingerprint limits must be positive integers")
    if max_files > 100000 or max_bytes > 1073741824:
        fail("fingerprint limits exceed supported bounds")
    root = os.path.abspath(project_root)
    records = []
    total_bytes = 0
    seen = 0
    reason = None
    root_flags = (os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                  | getattr(os, "O_NOFOLLOW", 0))
    try:
        root_fd = os.open(root, root_flags)
    except OSError as exc:
        return {"digest": None, "complete": False, "scope": scope,
                "files": 0, "bytes": 0, "reason": "unsafe_or_unavailable_project_root: " + str(exc)[:200]}

    def same_entry(left, right):
        return (left.st_dev, left.st_ino, left.st_mode, left.st_size, left.st_mtime_ns) == (
            right.st_dev, right.st_ino, right.st_mode, right.st_size, right.st_mtime_ns)

    def scan(directory_fd, prefix=""):
        nonlocal seen, total_bytes, reason
        try:
            names = sorted(os.listdir(directory_fd))
        except OSError as exc:
            reason = "scan_error: " + str(exc)[:200]
            return
        for name in names:
            relative = (prefix + "/" + name) if prefix else name
            if excluded(relative):
                continue
            seen += 1
            if seen > max_files:
                reason = "file_count_budget_exceeded"
                return
            try:
                info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                if stat.S_ISLNK(info.st_mode):
                    # dir_fd readlink reads only the link itself, never its target.
                    target = os.readlink(name, dir_fd=directory_fd)
                    item = {"path": relative, "kind": "symlink",
                            "target_sha256": hashlib.sha256(os.fsencode(target)).hexdigest()}
                elif stat.S_ISDIR(info.st_mode):
                    child_fd = os.open(name, root_flags, dir_fd=directory_fd)
                    try:
                        opened = os.fstat(child_fd)
                        if not same_entry(info, opened):
                            reason = "directory_changed_during_scan"
                            return
                        scan(child_fd, relative)
                        if reason:
                            return
                        after = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                        if not same_entry(info, after):
                            reason = "directory_changed_during_scan"
                            return
                    finally:
                        os.close(child_fd)
                    continue
                elif stat.S_ISREG(info.st_mode):
                    if total_bytes + info.st_size > max_bytes:
                        reason = "byte_budget_exceeded"
                        return
                    fd = os.open(name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                                 dir_fd=directory_fd)
                    digest = hashlib.sha256()
                    with os.fdopen(fd, "rb") as stream:
                        opened = os.fstat(stream.fileno())
                        if not same_entry(info, opened):
                            reason = "file_changed_during_scan"
                            return
                        read_bytes = 0
                        for block in iter(lambda: stream.read(1024 * 1024), b""):
                            read_bytes += len(block)
                            digest.update(block)
                        after_open = os.fstat(stream.fileno())
                    after_name = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                    if not same_entry(info, after_open) or not same_entry(info, after_name):
                        reason = "file_changed_during_scan"
                        return
                    if total_bytes + read_bytes > max_bytes:
                        reason = "byte_budget_exceeded"
                        return
                    total_bytes += read_bytes
                    item = {"path": relative, "kind": "file", "sha256": digest.hexdigest(),
                            "mode": stat.S_IMODE(info.st_mode) & 0o777}
                else:
                    item = {"path": relative, "kind": "other"}
            except OSError as exc:
                reason = "scan_error: " + str(exc)[:200]
                return
            records.append(item)

    try:
        scan(root_fd)
    finally:
        os.close(root_fd)
    complete = reason is None
    return {"digest": hashlib.sha256(canonical(records)).hexdigest() if complete else None,
            "complete": complete, "scope": scope, "files": len(records),
            "bytes": total_bytes, "reason": reason}

if __name__ == "__main__":
    if len(sys.argv) != 4 or sys.argv[1] != "fingerprint":
        fail("expected fingerprint, project root, and JSON arguments")
    args = json.loads(sys.argv[3])
    result = project_fingerprint(sys.argv[2], args["max_files"], args["max_bytes"])
    print(json.dumps(result, ensure_ascii=True, separators=(",", ":")))
'''

async def _request(executor, operation: str, project_root: str, payload: dict, *, output_budget: int):
    if not isinstance(project_root, str) or not project_root.startswith("/") or "\x00" in project_root:
        raise ValueError("project_root must be an absolute remote path")
    raw = json.dumps(payload, ensure_ascii=True, separators=(",", ":"))
    result = await executor.run_program(
        ["python3", "-c", _REMOTE, operation, project_root, raw],
        cwd=project_root,
        output_budget=output_budget,
        timeout=executor.config.file_timeout,
    )
    if getattr(result, "timed_out", False):
        raise ValueError("Project fingerprint operation timed out; inspect the operation record before retrying")
    raw_stdout = result.stdout
    output_id = getattr(result, "output_id", "")
    if output_id:
        try:
            stored = audit.directory(output_id) / "stdout"
            if stored.is_file():
                raw_stdout = stored.read_bytes().decode("utf-8", "replace")
        except (OSError, ValueError):
            pass
    if getattr(result, "truncated", False) and raw_stdout == result.stdout:
        raise ValueError("Project fingerprint result was truncated; reduce the scan budgets")
    try:
        value = json.loads(raw_stdout)
    except (TypeError, ValueError) as exc:
        detail = getattr(result, "stderr", "")
        raise ValueError((detail or "Project fingerprint returned invalid JSON")[:1000]) from exc
    if getattr(result, "exit_code", None) != 0 or not isinstance(value, dict):
        if isinstance(value, dict):
            raise ValueError(value.get("message") or value.get("error") or "Project fingerprint failed")
        raise ValueError("Project fingerprint returned invalid metadata")
    if output_id:
        value["output_id"] = output_id
    return value


async def project_fingerprint(executor, project_root: str, *, max_files: int = 10000,
                              max_bytes: int = 64 * 1024 * 1024) -> dict:
    """Return a bounded content fingerprint for an explicit caller.

    Ordinary command execution does not call this scanner. Callers opt in and
    provide file/byte budgets. Incomplete scans return ``digest=None`` and must
    not be used as proof that two project states match. Symlink targets are
    recorded without opening them.
    """
    if isinstance(max_files, bool) or not isinstance(max_files, int) or not 1 <= max_files <= 100000:
        raise ValueError("max_files must be between 1 and 100000")
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or not 1 <= max_bytes <= 1024 * 1024 * 1024:
        raise ValueError("max_bytes must be between 1 and 1073741824")
    return await _request(executor, "fingerprint", project_root,
                          {"max_files": max_files, "max_bytes": max_bytes}, output_budget=4096)
