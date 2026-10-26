"""Private local operation records and byte-bounded output retrieval."""
from __future__ import annotations

import contextvars
import fcntl
import json
import os
import re
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

operation_id = contextvars.ContextVar("operation_id", default=None)
_scoped_root = contextvars.ContextVar("audit_root", default=None)
_metadata = contextvars.ContextVar("audit_metadata", default=None)
_ID = re.compile(r"^[a-f0-9]{32}$")
STREAMS = {"stdout", "stderr", "stdin", "diff", "startup_stdout", "startup_stderr"}


def base_root() -> Path:
    """Resolve the configured audit root without creating it or a session."""
    return Path(os.environ.get(
        "REMOTE_WORKBENCH_AUDIT_DIR",
        Path(__file__).resolve().parent.parent / ".remote-workbench-audit",
    ))


@contextmanager
def scoped_root(path):
    """Bind audit operations in this context to a session-specific directory.

    Context variables keep concurrent MCP calls isolated without mutating the
    process-wide environment. Threads that serve the HTTP viewer should bind
    their own root explicitly with this context manager.
    """
    token = _scoped_root.set(Path(path).expanduser())
    try:
        yield
    finally:
        _scoped_root.reset(token)


@contextmanager
def scoped_metadata(**values):
    """Attach an immutable request's identity to every child record it creates."""
    token = _metadata.set(dict(values))
    try:
        yield
    finally:
        _metadata.reset(token)


def root() -> Path:
    path = _scoped_root.get()
    if path is None:
        path = base_root()
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    return path


def directory(record_id: str) -> Path:
    if not _ID.fullmatch(record_id):
        raise ValueError("Invalid operation/output ID")
    return root() / record_id


def create(**metadata) -> str:
    record_id = uuid.uuid4().hex
    directory(record_id).mkdir(mode=0o700)
    update(record_id, id=record_id, created_at=time.time(), **dict(metadata, **(_metadata.get() or {})))
    return record_id


def _edit(record_id: str, change) -> None:
    path = directory(record_id) / "record.json"
    lock = os.open(path.parent / ".record.lock", os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    temporary = path.with_name(".record-" + uuid.uuid4().hex + ".tmp")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX)
        current = json.loads(path.read_text()) if path.exists() else {}
        change(current)
        with open(temporary, "x", encoding="utf-8", opener=lambda p, f: os.open(p, f, 0o600)) as output:
            json.dump(current, output, ensure_ascii=False, indent=2, default=str)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
        os.close(lock)


def update(record_id: str, **values) -> None:
    _edit(record_id, lambda current: current.update(values))


def add_child(parent_id: str | None, child_id: str) -> None:
    if parent_id:
        _edit(parent_id, lambda current: current.update(
            child_commands=current.get("child_commands", []) + [child_id]))


def open_stream(record_id: str, stream: str):
    if stream not in STREAMS:
        raise ValueError("Unknown stream")
    return open(directory(record_id) / stream, "wb", opener=lambda p, f: os.open(p, f, 0o600))


def read_output(record_id: str, stream="stdout", cursor=0, max_bytes=8192, max_lines=None) -> dict:
    if stream not in STREAMS:
        raise ValueError("Unknown stream")
    cursor, max_bytes = int(cursor), int(max_bytes)
    if cursor < 0 or not 4 <= max_bytes <= 65536:
        raise ValueError("cursor >= 0 and 4 <= max_bytes <= 65536 required")
    if max_lines is not None and (not isinstance(max_lines, int) or isinstance(max_lines, bool) or
                                  not 1 <= max_lines <= 1000):
        raise ValueError("1 <= max_lines <= 1000 required")
    path = directory(record_id) / stream
    with path.open("rb") as source:
        source.seek(cursor)
        data = source.read(max_bytes)
    if max_lines is not None:
        lines = data.splitlines(keepends=True)
        if len(lines) > max_lines:
            data = b"".join(lines[:max_lines])
    # Preserve UTF-8 boundaries when reading the next page.
    text = data.decode("utf-8", errors="replace")
    if len(data) == max_bytes:
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            if exc.reason == "unexpected end of data":
                data = data[:exc.start]
            text = data.decode("utf-8", errors="replace")
    end = cursor + len(data)
    return {"output_id": record_id, "stream": stream, "content": text,
            "next_cursor": end, "total_bytes": path.stat().st_size,
            "truncated": end < path.stat().st_size}


def preview(data: bytes, budget: int, max_lines=200) -> tuple[str, bool]:
    """Head/tail preview, with both a byte and line cap (including long lines)."""
    lines = data.splitlines(keepends=True)
    limited = max_lines is not None and len(lines) > max_lines
    if limited:
        left = max(1, max_lines // 2)
        data = b"".join(lines[:left]) + b"\n[truncated lines]\n" + b"".join(lines[-left:])
    if len(data) > budget:
        marker = b"\n[truncated bytes]\n"
        half = max(0, (budget - len(marker)) // 2)
        data = data[:half] + marker + data[-half:] if half else marker[:budget]
        limited = True
    # Ignore partial boundary bytes, preserving the byte cap.
    return data.decode("utf-8", errors="ignore"), limited


def recent(limit=50) -> list[dict]:
    paths = sorted(root().glob("*/record.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    result = []
    for path in paths[:min(200, max(1, limit))]:
        try:
            value = json.loads(path.read_text())
            result.append({k: value[k] for k in ("id", "kind", "tool", "parent_id", "host", "cwd", "created_at", "status", "exit_code", "elapsed_ms", "thread_id", "workspace_id", "revision", "target_id") if k in value})
        except (OSError, ValueError):
            continue
    return result
