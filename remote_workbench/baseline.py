"""File-scoped preview and guarded delivery from a private session copy.

The remote implementation is stdlib-only because it runs through the host's
ordinary Python interpreter. Delivery is deliberately per-file: a preview
guard protects the selected source paths, but it is not a repository-wide
transaction and cannot serialize unrelated editors or shell processes.
"""
from __future__ import annotations

import json
import re

from remote_workbench import audit


_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_REMOTE = r'''import difflib
import hashlib
import json
import os
from pathlib import Path
import stat
import sys
import uuid

BASELINE_NAME = "baseline.json"
TOOL_OWNED = {".git", ".remote-workbench-state", ".remote-workbench-sessions"}
ENVIRONMENT_DIRS = {
    ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache",
    ".mypy_cache", ".ruff_cache", ".tox", ".nox", ".cache",
}
MAX_DIFF_BYTES = 1024 * 1024
MAX_TOTAL_DIFF_BYTES = 8 * 1024 * 1024

def fail(message, code="invalid_request"):
    print(json.dumps({"error": code, "message": str(message)[:1000]}, separators=(",", ":")))
    raise SystemExit(2)

def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode("utf-8")

def fingerprint(value):
    return hashlib.sha256(canonical(value)).hexdigest()

def inside(path, root):
    try:
        return os.path.commonpath([os.path.abspath(path), os.path.abspath(root)]) == os.path.abspath(root)
    except ValueError:
        return False

def excluded(relative):
    parts = relative.replace(os.sep, "/").split("/")
    return any(part in TOOL_OWNED or part in ENVIRONMENT_DIRS
               or part.startswith(".remote-workbench-write-")
               or part.startswith(".remote-workbench-download-") for part in parts)

def safe_relative(relative):
    if (not isinstance(relative, str) or not relative or "\x00" in relative
            or relative.startswith("/") or excluded(relative)):
        return False
    parts = relative.split("/")
    return all(part not in ("", ".", "..") for part in parts)

def open_parent(root, relative, create=False):
    if not safe_relative(relative):
        return None, None, "unsafe_path"
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        current = os.open(root, flags)
    except OSError:
        return None, None, "unsafe_project_root"
    parts = relative.split("/")
    for part in parts[:-1]:
        try:
            child = os.open(part, flags, dir_fd=current)
        except FileNotFoundError:
            if not create:
                os.close(current)
                return None, parts[-1], "missing_parent"
            try:
                os.mkdir(part, 0o700, dir_fd=current)
            except FileExistsError:
                pass
            try:
                child = os.open(part, flags, dir_fd=current)
            except OSError:
                os.close(current)
                return None, parts[-1], "unsafe_parent"
        except OSError:
            os.close(current)
            return None, parts[-1], "unsafe_parent"
        os.close(current)
        current = child
    return current, parts[-1], None

def file_state(root, relative, project_root=None, source_root=None):
    parent_fd, name, error = open_parent(root, relative)
    if error:
        if error == "missing_parent":
            return {"kind": "missing"}
        return {"kind": "unsafe", "reason": error}
    try:
        info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        os.close(parent_fd)
        return {"kind": "missing"}
    except OSError:
        os.close(parent_fd)
        return {"kind": "unsafe", "reason": "file_stat_failed"}
    try:
        if stat.S_ISLNK(info.st_mode):
            target = os.readlink(name, dir_fd=parent_fd)
            if os.path.isabs(target) and project_root and source_root:
                path = os.path.join(root, relative)
                resolved = os.path.realpath(path)
                if inside(resolved, source_root):
                    target = os.path.join(project_root, os.path.relpath(resolved, source_root))
            return {"kind": "symlink", "target_sha256": hashlib.sha256(os.fsencode(target)).hexdigest()}
        if not stat.S_ISREG(info.st_mode):
            return {"kind": "other"}
        special = bool(info.st_mode & (stat.S_ISUID | stat.S_ISGID | stat.S_ISVTX))
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(name, flags, dir_fd=parent_fd)
        except OSError:
            return {"kind": "unsafe", "reason": "file_open_failed"}
        digest = hashlib.sha256()
        try:
            with os.fdopen(fd, "rb") as stream:
                opened = os.fstat(stream.fileno())
                if not stat.S_ISREG(opened.st_mode):
                    return {"kind": "unsafe", "reason": "file_changed_during_read"}
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(block)
        except OSError:
            return {"kind": "unsafe", "reason": "file_read_failed"}
        return {"kind": "file", "sha256": digest.hexdigest(),
                "mode": stat.S_IMODE(info.st_mode) & 0o777, "special_mode": special}
    finally:
        os.close(parent_fd)

def inventory(root):
    found = set()
    for current, dirs, files in os.walk(root, topdown=True, followlinks=False):
        relative_dir = os.path.relpath(current, root)
        if relative_dir == ".":
            relative_dir = ""
        selected_dirs = []
        for name in dirs:
            relative = os.path.join(relative_dir, name) if relative_dir else name
            path = os.path.join(current, name)
            if excluded(relative):
                continue
            if os.path.islink(path):
                found.add(relative.replace(os.sep, "/"))
                continue
            selected_dirs.append(name)
        dirs[:] = sorted(selected_dirs)
        for name in files:
            relative = os.path.join(relative_dir, name) if relative_dir else name
            if not excluded(relative):
                found.add(relative.replace(os.sep, "/"))
    return found

def read_manifest(project_root):
    project_root = os.path.abspath(project_root)
    try:
        root_info = os.lstat(project_root)
    except OSError:
        fail("The private session project is unavailable", "baseline_unavailable")
    if stat.S_ISLNK(root_info.st_mode) or not stat.S_ISDIR(root_info.st_mode):
        fail("The private session project is not a real directory", "baseline_unavailable")
    parent = os.path.dirname(project_root)
    marker = os.path.join(parent, BASELINE_NAME)
    if os.path.islink(marker) or not os.path.isfile(marker):
        fail("This session has no usable initial content baseline", "baseline_unavailable")
    try:
        with open(marker, "r", encoding="utf-8") as stream:
            value = json.load(stream)
    except (OSError, ValueError) as exc:
        fail("Could not read the session content baseline: " + str(exc), "baseline_invalid")
    if not isinstance(value, dict) or value.get("version") != 1:
        fail("Unsupported session content baseline", "baseline_invalid")
    if value.get("project_root") != project_root or value.get("status") != "ready":
        fail("The content baseline does not belong to this private project", "baseline_invalid")
    baseline_id = value.get("baseline_id")
    without_id = dict(value)
    without_id.pop("baseline_id", None)
    if not isinstance(baseline_id, str) or fingerprint(without_id) != baseline_id:
        fail("The session content baseline failed its integrity check", "baseline_invalid")
    if not isinstance(value.get("files"), dict) or not isinstance(value.get("source_root"), str):
        fail("The session content baseline is incomplete", "baseline_invalid")
    return value

def state_id(value):
    return fingerprint(value)

def change_type(before, after):
    if before.get("kind") == "missing":
        return "added"
    if after.get("kind") == "missing":
        return "deleted"
    return "modified"

def text_bytes(root, relative, expected):
    if expected.get("kind") == "missing":
        return b""
    if expected.get("kind") != "file" or expected.get("special_mode"):
        return None
    parent_fd, name, error = open_parent(root, relative)
    if error:
        return None
    try:
        fd = os.open(name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=parent_fd)
        with os.fdopen(fd, "rb") as stream:
            content = stream.read()
    except OSError:
        return None
    finally:
        os.close(parent_fd)
    actual = file_state(root, relative)
    if actual != expected:
        return None
    if b"\x00" in content:
        return None
    try:
        content.decode("utf-8")
    except UnicodeDecodeError:
        return None
    return content

def describe(project_root, manifest, selected, want_diff):
    source_root = manifest["source_root"]
    entries = manifest["files"]
    changed_paths = inventory(project_root) | set(entries)
    if selected is not None:
        for path in selected:
            if not safe_relative(path):
                fail("A selected path is unsafe or outside the copied project", "unsafe_path")
        changed_paths = set(selected)
    result = []
    total_diff_bytes = 0
    for relative in sorted(changed_paths):
        record = entries.get(relative, {"source": {"kind": "missing"},
                                          "project": {"kind": "missing"},
                                          "stable_at_copy": True})
        base_project = record.get("project", {"kind": "missing"})
        base_source = record.get("source", {"kind": "missing"})
        session_now = file_state(project_root, relative)
        if session_now == base_project:
            continue
        source_now = file_state(source_root, relative, project_root, source_root)
        conflict = (record.get("stable_at_copy") is not True or source_now != base_source)
        supported = all(state.get("kind") in ("file", "missing") and not state.get("special_mode")
                        for state in (base_project, base_source, session_now, source_now))
        item = {
            "path": relative,
            "change": change_type(base_project, session_now),
            "mode_change": (base_project.get("kind") == session_now.get("kind") == "file"
                            and base_project.get("mode") != session_now.get("mode")),
            "source_conflict": conflict,
            "supported": supported,
            "reason": ("source_changed_since_session_start" if conflict else
                       None if supported else "only_regular_utf8_text_files_are_supported"),
            "baseline_state": base_project,
            "source_state": source_now,
            "session_state": session_now,
            "guard": {"baseline_id": manifest["baseline_id"],
                      "baseline": state_id(base_project),
                      "source": state_id(source_now),
                      "session": state_id(session_now)},
        }
        if want_diff and supported and not conflict:
            # The source bytes are the original private-copy bytes only while
            # its full state still matches the recorded initial source state.
            before = text_bytes(source_root, relative, source_now)
            after = text_bytes(project_root, relative, session_now)
            if before is None or after is None:
                item["supported"] = False
                item["reason"] = "file_changed_while_previewing"
            else:
                old_lines = before.decode("utf-8").splitlines(keepends=True)
                new_lines = after.decode("utf-8").splitlines(keepends=True)
                patch = "".join(difflib.unified_diff(
                    old_lines, new_lines,
                    fromfile="a/" + relative, tofile="b/" + relative,
                ))
                patch_size = len(patch.encode("utf-8"))
                if patch_size > MAX_DIFF_BYTES:
                    item["supported"] = False
                    item["reason"] = "preview_diff_exceeds_1_mib"
                elif total_diff_bytes + patch_size > MAX_TOTAL_DIFF_BYTES:
                    item["supported"] = False
                    item["reason"] = "preview_total_diff_exceeds_8_mib"
                else:
                    item["diff"] = patch
                    total_diff_bytes += patch_size
        elif want_diff and conflict:
            item["reason"] = "source_changed_since_session_start; diff omitted to avoid a false baseline"
        result.append(item)
    return {"status": "ready", "baseline_id": manifest["baseline_id"], "files": result}

def apply(project_root, manifest, files):
    if not isinstance(files, list) or not files:
        fail("Select one or more reviewed file previews to apply", "invalid_request")
    preview = describe(project_root, manifest, [item.get("path") if isinstance(item, dict) else None
                                               for item in files], False)
    current = {item["path"]: item for item in preview["files"]}
    outcomes = []
    for requested in files:
        if not isinstance(requested, dict) or set(requested) != {"path", "guard"}:
            outcomes.append({"path": None, "status": "rejected", "reason": "missing_preview_guard"})
            continue
        relative, guard = requested["path"], requested["guard"]
        item = current.get(relative)
        if item is None:
            outcomes.append({"path": relative, "status": "unchanged", "reason": "no_session_change"})
            continue
        if not isinstance(guard, dict) or guard != item["guard"]:
            outcomes.append({"path": relative, "status": "conflict", "reason": "preview_guard_stale"})
            continue
        if item["source_conflict"]:
            outcomes.append({"path": relative, "status": "conflict", "reason": "source_changed_since_session_start"})
            continue
        if not item["supported"]:
            outcomes.append({"path": relative, "status": "rejected", "reason": item["reason"]})
            continue
        source_root = manifest["source_root"]
        source_expected = manifest["files"].get(relative, {}).get("source", {"kind": "missing"})
        source_now = file_state(source_root, relative, project_root, source_root)
        session_now = file_state(project_root, relative)
        if source_now != source_expected or state_id(source_now) != guard.get("source"):
            outcomes.append({"path": relative, "status": "conflict", "reason": "source_changed_since_preview"})
            continue
        if state_id(session_now) != guard.get("session"):
            outcomes.append({"path": relative, "status": "conflict", "reason": "session_file_changed_since_preview"})
            continue
        content = text_bytes(project_root, relative, session_now)
        if content is None:
            outcomes.append({"path": relative, "status": "rejected", "reason": "only_regular_utf8_text_files_are_supported"})
            continue
        parent_fd, target_name, error = open_parent(source_root, relative, create=True)
        if error:
            outcomes.append({"path": relative, "status": "conflict", "reason": error})
            continue
        # Recheck immediately before the single-file replacement. This is a
        # hash guard, not a filesystem-wide lock or multi-file transaction.
        temporary_name = None
        try:
            if file_state(source_root, relative, project_root, source_root) != source_expected:
                outcomes.append({"path": relative, "status": "conflict", "reason": "source_changed_before_write"})
                continue
            if session_now.get("kind") == "missing":
                os.unlink(target_name, dir_fd=parent_fd)
                outcomes.append({"path": relative, "status": "applied", "change": "deleted"})
                continue
            temporary_name = ".remote-workbench-write-" + uuid.uuid4().hex
            fd = os.open(temporary_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                         | getattr(os, "O_NOFOLLOW", 0), 0o600, dir_fd=parent_fd)
            with os.fdopen(fd, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
                os.fchmod(stream.fileno(), session_now.get("mode", 0o600) & 0o777)
            if file_state(source_root, relative, project_root, source_root) != source_expected:
                raise FileExistsError("source changed before file replacement")
            os.replace(temporary_name, target_name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
            temporary_name = None
            try:
                os.fsync(parent_fd)
            except OSError:
                pass
        except FileExistsError:
            outcomes.append({"path": relative, "status": "conflict", "reason": "source_changed_before_file_replacement"})
        except OSError as exc:
            outcomes.append({"path": relative, "status": "failed", "reason": str(exc)[:300]})
        else:
            outcomes.append({"path": relative, "status": "applied", "change": item["change"]})
        finally:
            if temporary_name:
                try:
                    os.unlink(temporary_name, dir_fd=parent_fd)
                except FileNotFoundError:
                    pass
            os.close(parent_fd)
    return {"status": "finished", "baseline_id": manifest["baseline_id"], "files": outcomes,
            "transaction_scope": "independent_files"}

def project_fingerprint(project_root, max_files, max_bytes):
    scope = "non-tool-owned files in the private project; excludes .git and runtime directories; symlinks are hashed without following"
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

def main():
    if len(sys.argv) != 4:
        fail("expected operation, project root, and JSON arguments")
    operation, project_root, raw = sys.argv[1:]
    project_root = os.path.abspath(project_root)
    try:
        args = json.loads(raw)
    except ValueError:
        fail("request JSON is invalid")
    if operation == "fingerprint":
        max_files = args.get("max_files", 10000) if isinstance(args, dict) else 10000
        max_bytes = args.get("max_bytes", 64 * 1024 * 1024) if isinstance(args, dict) else 64 * 1024 * 1024
        value = project_fingerprint(project_root, max_files, max_bytes)
        print(json.dumps(value, ensure_ascii=True, separators=(",", ":")))
        return
    manifest = read_manifest(project_root)
    if operation == "list":
        value = describe(project_root, manifest, None, False)
    elif operation == "preview":
        paths = args.get("paths") if isinstance(args, dict) else None
        if not isinstance(paths, list) or not paths:
            fail("Preview requires an explicit nonempty list of relative file paths")
        value = describe(project_root, manifest, paths, True)
    elif operation == "apply":
        files = args.get("files") if isinstance(args, dict) else None
        value = apply(project_root, manifest, files)
    else:
        fail("unknown operation")
    print(json.dumps(value, ensure_ascii=True, separators=(",", ":")))

if __name__ == "__main__":
    main()
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
        raise ValueError("Session delivery operation timed out; inspect the operation record before retrying")
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
        raise ValueError("Session delivery result was truncated; narrow the selected file paths")
    try:
        value = json.loads(raw_stdout)
    except (TypeError, ValueError) as exc:
        detail = getattr(result, "stderr", "")
        raise ValueError((detail or "Session delivery returned invalid JSON")[:1000]) from exc
    if getattr(result, "exit_code", None) != 0 or not isinstance(value, dict):
        if isinstance(value, dict):
            raise ValueError(value.get("message") or value.get("error") or "Session delivery failed")
        raise ValueError("Session delivery returned invalid metadata")
    if output_id:
        value["output_id"] = output_id
    if operation == "preview":
        files = value.get("files", [])
        all_diffs = []
        for item in files:
            patch = item.get("diff") if isinstance(item, dict) else None
            if isinstance(patch, str) and patch:
                all_diffs.append(patch)
        full_diff = "".join(all_diffs)
        parent_id = audit.operation_id.get()
        if parent_id:
            with audit.open_stream(parent_id, "diff") as stream:
                stream.write(full_diff.encode("utf-8"))
            value["diff_output_id"] = parent_id
        byte_budget = payload.get("max_bytes", 8192)
        used = 0
        for item in files:
            if not isinstance(item, dict) or not isinstance(item.get("diff"), str):
                continue
            patch_bytes = item["diff"].encode("utf-8")
            if used + len(patch_bytes) > byte_budget:
                item.pop("diff", None)
                item["diff_omitted"] = True
                item["diff_bytes"] = len(patch_bytes)
            else:
                used += len(patch_bytes)
        value["diff_bytes_returned"] = used
        value["diff_bytes_saved"] = len(full_diff.encode("utf-8"))
    return value


async def preview_delivery(executor, project_root: str, paths: list[str] | None = None,
                           *, max_bytes: int = 8192) -> dict:
    """List changes, or preview selected changed files with hash guards.

    With ``paths=None`` the response contains changed paths and conflict status
    without diff text. Selecting explicit paths returns UTF-8 unified diffs and
    the guards required by :func:`apply_delivery`.
    """
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or not 1 <= max_bytes <= 8192:
        raise ValueError("max_bytes must be between 1 and 8192")
    if paths is None:
        return await _request(executor, "list", project_root, {}, output_budget=256 * 1024)
    if not isinstance(paths, list) or any(not isinstance(path, str) for path in paths):
        raise ValueError("paths must be a list of relative file names")
    return await _request(executor, "preview", project_root,
                          {"paths": paths, "max_bytes": max_bytes}, output_budget=1024 * 1024)


async def apply_delivery(executor, project_root: str, files: list[dict]) -> dict:
    """Apply selected preview entries after per-file source and session checks.

    Pass ``files`` entries copied from ``preview_delivery(..., paths=...)``.
    Each file needs its original ``path`` and returned ``guard``. Conflicted
    paths are rejected individually; successful paths are replaced atomically.
    """
    if not isinstance(files, list) or any(not isinstance(item, dict) for item in files):
        raise ValueError("files must contain reviewed preview entries")
    selected = [{"path": item.get("path"), "guard": item.get("guard")} for item in files]
    if any(not isinstance(item["path"], str) or not isinstance(item["guard"], dict) for item in selected):
        raise ValueError("each selected file must include the preview path and hash guard")
    return await _request(executor, "apply", project_root, {"files": selected}, output_budget=64 * 1024)


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
