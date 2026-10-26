"""Persistent local job handles and autonomous, read-only SSH log collection."""
from __future__ import annotations

import asyncio
import base64
from contextlib import contextmanager
import contextvars
import fcntl
import hashlib
import json
import os
from pathlib import Path
import posixpath
import re
import time
import uuid

from remote_workbench import audit
from remote_workbench.remote_shell import python_command

_JOB_ID = re.compile(r"^[a-zA-Z0-9_-]{1,80}$")
_ORIGIN_NONCE = re.compile(r"^[a-f0-9]{32}$")
_job_origin = contextvars.ContextVar("remote_workbench_job_origin", default=None)


def _public_remote_task(value):
    if not isinstance(value, dict):
        return value
    return {key: item for key, item in value.items() if key != "origin_nonce"}


@contextmanager
def scoped_origin(origin):
    """Bind a registry-issued job origin to one async tool call."""
    token = _job_origin.set(dict(origin) if origin is not None else None)
    try:
        yield
    finally:
        _job_origin.reset(token)


def current_origin(executor=None, job_id=None):
    origin = _job_origin.get()
    if not isinstance(origin, dict):
        return None
    if job_id is not None and origin.get("job_id") != job_id:
        return None
    return dict(origin)


def _handle_path(executor, job_id):
    if not _JOB_ID.fullmatch(job_id):
        raise ValueError("Invalid job ID")
    identity = [executor.config.ssh_target, executor.config.default_cwd,
                executor.remote_state_dir, job_id]
    key = hashlib.sha256(json.dumps(identity).encode()).hexdigest()
    return audit.root() / ".job-handles" / (key + ".json")


def local_info(executor, job_id):
    path = _handle_path(executor, job_id)
    try:
        if path.is_symlink():
            return {"error": "local_job_identity_unconfirmed", "job_id": job_id,
                    "sync_state": "disconnected", "execution_state": "unknown",
                    "sync_error": "Local job handle is a symbolic link"}
        handle = json.loads(path.read_text())
        record = json.loads((audit.directory(handle["job_output_id"]) / "record.json").read_text())
        result = {key: record.get(key) for key in (
            "running", "exit_code", "execution_state", "sync_state", "remote_task", "sync_error", "synced_at",
            "actual_cwd")}
        result["remote_task"] = _public_remote_task(result.get("remote_task"))
        result.update(job_id=job_id, job_output_id=handle["job_output_id"])
        for stream in ("stdout", "stderr"):
            result[stream + "_bytes"] = (audit.directory(handle["job_output_id"]) / stream).stat().st_size
        return result
    except FileNotFoundError:
        return {"error": "local_job_not_found", "job_id": job_id}


def _unknown(job_id, reason, *, error="job_identity_unconfirmed"):
    return {"job_id": job_id, "error": error, "running": None,
            "execution_state": "unknown", "sync_state": "disconnected",
            "sync_error": reason, "state_reason": reason}


def _expected_job_dir(origin, job_id):
    state_dir = origin.get("remote_state_dir")
    return posixpath.normpath(posixpath.join(state_dir or "", "jobs", job_id))


def _validate_target_descriptor(executor, job_id, origin):
    if not isinstance(origin, dict):
        return "recovery_descriptor_incomplete"
    if origin.get("job_id") != job_id:
        return "job_id_mismatch"
    if not isinstance(origin.get("host"), str) or not origin["host"]:
        return "origin_host_alias_unavailable"
    if not isinstance(origin.get("ssh_target"), str) or not origin["ssh_target"]:
        return "origin_ssh_target_unavailable"
    if origin["ssh_target"] != executor.config.ssh_target:
        return "origin_ssh_target_mismatch"
    if not all(isinstance(origin.get(name), str) and origin[name] for name in
               ("workspace_id", "target_id", "source_cwd", "target_cwd", "project_root")):
        return "target_origin_incomplete"
    if origin["target_cwd"] != executor.config.default_cwd:
        return "origin_target_cwd_mismatch"
    if origin.get("remote_state_dir") is not None and (
            not isinstance(origin.get("remote_state_dir"), str) or
            not origin["remote_state_dir"].startswith("/") or
            executor.remote_state_dir != origin["remote_state_dir"]):
        return "remote_state_dir_unavailable_or_changed"
    return None


def _validate_descriptor(executor, job_id, origin):
    reason = _validate_target_descriptor(executor, job_id, origin)
    if reason:
        return reason
    if not isinstance(origin.get("origin_nonce"), str) or not _ORIGIN_NONCE.fullmatch(origin["origin_nonce"]):
        return "origin_nonce_unavailable"
    if not isinstance(origin.get("launch_cwd"), str) or not origin["launch_cwd"].startswith("/"):
        return "launch_cwd_unavailable"
    if (not isinstance(origin.get("remote_state_dir"), str) or
            not origin["remote_state_dir"].startswith("/") or
            executor.remote_state_dir != origin["remote_state_dir"]):
        return "remote_state_dir_unavailable_or_changed"
    return None


def validate_origin(executor, job_id, origin):
    """Validate a context bound by the session router before any remote call."""
    return _validate_descriptor(executor, job_id, origin)


def _remote_identity_matches(value, origin, job_id):
    expected_dir = _expected_job_dir(origin, job_id)
    task = value.get("remote_task") if isinstance(value, dict) else None
    if not isinstance(task, dict):
        return "remote_origin_status_unavailable"
    if value.get("job_dir") != expected_dir or task.get("task_dir") != expected_dir:
        return "task_dir_mismatch"
    if task.get("task_id") != job_id:
        return "task_id_mismatch"
    legacy_identity = origin.get("_legacy_identity")
    if legacy_identity is not None:
        for key in ("node", "boot_id", "pid", "start_token", "task_id", "task_dir", "version"):
            if key in legacy_identity and task.get(key) != legacy_identity.get(key):
                return "saved_process_identity_mismatch"
    else:
        if task.get("origin_nonce") != origin.get("origin_nonce"):
            return "origin_nonce_mismatch"
        if task.get("launch_cwd") != origin.get("launch_cwd"):
            return "launch_cwd_mismatch"
        if not isinstance(task.get("actual_cwd"), str) or not task["actual_cwd"].startswith("/"):
            return "actual_cwd_unavailable"
    if origin.get("actual_cwd") is not None and task.get("actual_cwd") != origin["actual_cwd"]:
        return "actual_cwd_mismatch"
    if (not isinstance(task.get("node"), str) or not task["node"] or
            (not task.get("boot_id") and not str(task.get("start_token", "")).startswith("ps:")) or
            not isinstance(task.get("pid"), int) or isinstance(task.get("pid"), bool) or
            task["pid"] <= 0 or not isinstance(task.get("start_token"), str) or
            not task["start_token"]):
        return "remote_process_identity_incomplete"
    return None


def _read_handle(path):
    if path.is_symlink():
        raise ValueError("Local job handle is a symbolic link")
    return json.loads(path.read_text())


def _handle_matches(handle, origin, job_id):
    saved = handle.get("origin")
    if isinstance(saved, dict):
        for key in ("workspace_id", "target_id", "host", "source_cwd", "target_cwd", "project_root",
                    "ssh_target"):
            if saved.get(key) != origin.get(key):
                return key + "_mismatch"
        if origin.get("_legacy_identity") is None:
            for key in ("origin_nonce", "launch_cwd", "remote_state_dir"):
                if saved.get(key) != origin.get(key):
                    return key + "_mismatch"
    elif origin.get("_legacy_identity") is None:
        return "local_origin_unavailable"
    if handle.get("job_id") != job_id or handle.get("job_dir") != _expected_job_dir(origin, job_id):
        return "task_dir_mismatch"
    return _remote_identity_matches({"job_dir": handle.get("job_dir"),
                                    "remote_task": handle.get("remote_task")}, origin, job_id)


def _legacy_handle_origin(executor, job_id, origin, handle):
    """Recover only identity already saved in a pre-nonce local handle/record."""
    try:
        if not isinstance(handle, dict) or handle.get("job_id") != job_id:
            return None, None, "local_handle_identity_unavailable"
        job_dir = handle.get("job_dir")
        if not isinstance(job_dir, str) or not posixpath.isabs(job_dir):
            return None, None, "saved_job_dir_unavailable"
        expected_state_dir = posixpath.dirname(posixpath.dirname(posixpath.normpath(job_dir)))
        if posixpath.normpath(job_dir) != posixpath.join(expected_state_dir, "jobs", job_id):
            return None, None, "saved_job_dir_mismatch"
        if origin.get("remote_state_dir") not in (None, expected_state_dir):
            return None, None, "saved_remote_state_dir_mismatch"
        if executor.remote_state_dir != expected_state_dir:
            return None, None, "saved_remote_state_dir_changed"
        record_id = handle.get("job_output_id")
        record = json.loads((audit.directory(record_id) / "record.json").read_text())
        for key, record_key in (("workspace_id", "workspace_id"), ("target_id", "target_id")):
            if record.get(record_key) != origin.get(key):
                return None, None, "saved_" + key + "_mismatch"
        saved_endpoint = record.get("host")
        if origin.get("ssh_target") not in (None, saved_endpoint):
            return None, None, "saved_ssh_target_mismatch"
        if saved_endpoint != executor.config.ssh_target:
            return None, None, "saved_ssh_target_changed"
        launch_cwd = record.get("cwd")
        if not isinstance(launch_cwd, str) or not launch_cwd.startswith("/"):
            return None, None, "saved_launch_cwd_unavailable"
        if origin.get("launch_cwd") not in (None, launch_cwd):
            return None, None, "saved_launch_cwd_mismatch"
        if record.get("remote_job_dir") != job_dir:
            return None, None, "saved_job_dir_mismatch"
        task = record.get("remote_task")
        if not isinstance(task, dict):
            return None, None, "saved_process_identity_unavailable"
        identity = {key: task.get(key) for key in
                    ("node", "boot_id", "pid", "start_token", "task_id", "task_dir", "version")
                    if key in task}
        required = ("node", "pid", "start_token", "task_id", "task_dir", "version")
        if any(identity.get(key) in (None, "") for key in required):
            return None, None, "saved_process_identity_incomplete"
        if identity["task_id"] != job_id or identity["task_dir"] != job_dir:
            return None, None, "saved_process_identity_mismatch"
        if (not identity.get("boot_id") and
                not str(identity.get("start_token", "")).startswith("ps:")):
            return None, None, "saved_boot_identity_unavailable"
        saved_origin = handle.get("origin")
        nonce = (saved_origin.get("origin_nonce") if isinstance(saved_origin, dict) else
                 task.get("origin_nonce"))
        enriched = dict(origin, launch_cwd=launch_cwd, remote_state_dir=expected_state_dir,
                        origin_nonce=nonce, ssh_target=saved_endpoint,
                        _legacy_identity=None if nonce else identity)
        handle = dict(handle, remote_task=task, origin=enriched)
        reason = (_validate_descriptor(executor, job_id, enriched) if nonce else
                  _validate_target_descriptor(executor, job_id, enriched))
        if reason:
            return None, None, reason
        return enriched, handle, None
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        return None, None, "saved_local_identity_unavailable"


def origin_for_control(executor, job_id, origin):
    """Return a safe remote control descriptor, or an unknown result for legacy origins."""
    if origin is None:
        return None
    if origin.get("origin_nonce"):
        reason = _validate_descriptor(executor, job_id, origin)
        return _unknown(job_id, reason) if reason else dict(origin)
    try:
        path = _handle_path(executor, job_id)
        handle = _read_handle(path)
    except (OSError, ValueError, KeyError, json.JSONDecodeError):
        return _unknown(job_id, "origin_nonce_unavailable_and_no_local_identity")
    enriched, _, reason = _legacy_handle_origin(executor, job_id, origin, handle)
    return _unknown(job_id, reason) if reason else enriched


def _install_handle(executor, result, command, origin):
    job_id = result["job_id"]
    path = _handle_path(executor, job_id)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock = os.open(path.with_suffix(".create.lock"), os.O_CREAT | os.O_RDWR |
                   getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if path.exists() or path.is_symlink():
            handle = _read_handle(path)
            if origin is not None:
                reason = _handle_matches(handle, origin, job_id)
                if reason:
                    return None, reason
            return handle, None

        remote_task = result.get("remote_task")
        if origin is not None:
            reason = _remote_identity_matches(result, origin, job_id)
            if reason:
                return None, reason
        output_id = audit.create(kind="job", parent_id=audit.operation_id.get(),
            host=executor.config.ssh_target, cwd=origin.get("launch_cwd") if origin else result.get("launch_cwd", executor.config.default_cwd),
            command=command or "[recovered background job]", job_id=job_id,
            remote_job_dir=result["job_dir"], remote_task=remote_task,
            running=result.get("running", True), exit_code=result.get("exit_code"),
            execution_state=result.get("execution_state", "running"), sync_state="queued", status="running",
            origin_nonce=origin.get("origin_nonce") if origin else None,
            launch_cwd=origin.get("launch_cwd") if origin else result.get("launch_cwd"),
            remote_state_dir=origin.get("remote_state_dir") if origin else executor.remote_state_dir,
            actual_cwd=(remote_task.get("actual_cwd") if isinstance(remote_task, dict) else None))
        audit.add_child(audit.operation_id.get(), output_id)
        for stream in ("stdout", "stderr"):
            with audit.open_stream(output_id, stream):
                pass
        handle = dict(job_id=job_id, job_dir=result["job_dir"], job_output_id=output_id,
                      remote_task=remote_task)
        if origin is not None:
            handle["origin"] = dict(origin)
        temporary = path.with_suffix("." + uuid.uuid4().hex + ".tmp")
        with open(temporary, "x", opener=lambda p, f: os.open(p, f, 0o600)) as output:
            json.dump(handle, output)
            output.flush()
            os.fsync(output.fileno())
        temporary.replace(path)
        return handle, None
    finally:
        os.close(lock)


def _persist_handle_identity(path, handle):
    lock = os.open(path.with_suffix(".create.lock"), os.O_CREAT | os.O_RDWR |
                   getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX)
        current = _read_handle(path)
        if (current.get("job_id") != handle.get("job_id") or
                current.get("job_output_id") != handle.get("job_output_id")):
            raise ValueError("Local job handle changed during identity recovery")
        current["origin"] = handle["origin"]
        current["remote_task"] = handle["remote_task"]
        temporary = path.with_suffix("." + uuid.uuid4().hex + ".tmp")
        try:
            with open(temporary, "x", opener=lambda p, f: os.open(p, f, 0o600)) as output:
                json.dump(current, output)
                output.flush()
                os.fsync(output.fileno())
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)
    finally:
        os.close(lock)


async def track(executor, result, command, cwd, origin=None):
    job_id = result.get("job_id")
    if not isinstance(job_id, str) or not _JOB_ID.fullmatch(job_id):
        return _unknown(job_id or "", "job_id_unavailable")
    origin = origin or current_origin(executor, job_id)
    if origin is not None:
        reason = _validate_descriptor(executor, job_id, origin)
        if reason:
            return _unknown(job_id, reason)
    handle, reason = _install_handle(executor, result, command, origin)
    if reason:
        return _unknown(job_id, reason)
    if origin is None:
        return await ensure(executor, job_id)
    return await ensure(executor, job_id, origin=origin)


async def _recover(executor, job_id, origin):
    reason = _validate_descriptor(executor, job_id, origin)
    if reason:
        return _unknown(job_id, reason)
    try:
        from remote_workbench.remote_client import edit
        value = await edit(executor, {
            "op": "job_status", "job_id": job_id, "stdout_cursor": 0,
            "stderr_cursor": 0, "budget": 256, "tail_lines": 1,
            "expected_origin_nonce": origin["origin_nonce"],
            "expected_launch_cwd": origin["launch_cwd"],
            "expected_remote_state_dir": origin["remote_state_dir"],
            "expected_actual_cwd": origin.get("actual_cwd"),
        }, cwd=origin["target_cwd"], max_bytes=256)
    except (OSError, TimeoutError, ValueError, asyncio.TimeoutError) as exc:
        return _unknown(job_id, "recovery_connection_unavailable: " + str(exc))
    if value.get("error"):
        return _unknown(job_id, value.get("state_reason") or value.get("message") or value["error"])
    reason = _remote_identity_matches(value, origin, job_id)
    if reason:
        return _unknown(job_id, reason)
    handle, reason = _install_handle(executor, value, "[recovered background job]", origin)
    if reason:
        return _unknown(job_id, reason)
    return handle


async def ensure(executor, job_id, origin=None):
    if not _JOB_ID.fullmatch(job_id):
        return _unknown(job_id, "invalid_job_id")
    origin = origin or current_origin(executor, job_id)
    path = _handle_path(executor, job_id)
    try:
        handle = _read_handle(path)
    except FileNotFoundError:
        if origin is None:
            return local_info(executor, job_id)
        recovered = await _recover(executor, job_id, origin)
        if recovered.get("error"):
            return recovered
        handle = recovered
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        return _unknown(job_id, "local_handle_unavailable: " + str(exc))

    if origin is not None:
        if origin.get("origin_nonce") is None:
            enriched, updated_handle, reason = _legacy_handle_origin(executor, job_id, origin, handle)
            if reason:
                return _unknown(job_id, reason)
            try:
                _persist_handle_identity(path, updated_handle)
            except (OSError, ValueError) as exc:
                return _unknown(job_id, "local_handle_identity_write_failed: " + str(exc))
            origin, handle = enriched, updated_handle
        descriptor_check = (_validate_target_descriptor(executor, job_id, origin)
                            if origin.get("_legacy_identity") is not None else
                            _validate_descriptor(executor, job_id, origin))
        reason = descriptor_check or _handle_matches(handle, origin, job_id)
        if reason:
            return _unknown(job_id, reason)
    info = local_info(executor, job_id)
    if info.get("error"):
        return info
    if info.get("sync_state") == "complete":
        return local_info(executor, job_id)
    tasks = getattr(executor, "_log_collectors", None)
    if tasks is None:
        tasks = executor._log_collectors = {}
    key = str(path)
    if key not in tasks or tasks[key].done():
        tasks[key] = asyncio.create_task(_collect(executor, path), name="rwb-log-" + job_id)
        await asyncio.sleep(0)
    return local_info(executor, job_id)


async def close(executor):
    tasks = list(getattr(executor, "_log_collectors", {}).values())
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


async def _collect(executor, path):
    lock = None
    process = heartbeat = errors = None
    record_id = None
    try:
        # Read the already-atomically-published handle before opening the
        # inter-process lock, so even a lock-file permission error can mark
        # this local record unknown instead of leaving it indefinitely queued.
        handle = json.loads(path.read_text())
        record_id = handle["job_output_id"]
        record_dir = audit.directory(record_id)
        lock = os.open(path.with_suffix(".lock"), os.O_CREAT | os.O_RDWR
                       | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return  # Another MCP process owns this exact log stream.
        cursors = {stream: (record_dir / stream).stat().st_size for stream in ("stdout", "stderr")}
        settings = dict(job_dir=handle["job_dir"], cursors=cursors,
                        prefix="RWB_FOLLOW_" + uuid.uuid4().hex + ":")
        origin = handle.get("origin")
        if isinstance(origin, dict):
            settings.update(expected_origin_nonce=origin.get("origin_nonce"),
                            expected_launch_cwd=(None if origin.get("_legacy_identity") is not None
                                                 else origin.get("launch_cwd")),
                            expected_remote_state_dir=origin.get("remote_state_dir"),
                            expected_actual_cwd=origin.get("actual_cwd"),
                            expected_job_id=handle.get("job_id"))
            if origin.get("_legacy_identity") is not None:
                settings["expected_identity"] = origin["_legacy_identity"]
        source = ("login_spec = None\n" + Path(__file__).with_name("remote_task.py").read_text()
                  + "\n" + Path(__file__).with_name("remote_log_follow.py").read_text()
                  + "\nfollow_logs(" + repr(settings) + ")\n")
        audit.update(record_id, sync_state="syncing", sync_error=None)
        process = await asyncio.create_subprocess_exec(*executor._ssh_args(python_command(source)),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            limit=131072)

        async def pulse():
            while True:
                process.stdin.write(b".\n")
                await process.stdin.drain()
                await asyncio.sleep(3)

        async def drain_errors():
            tail = b""
            while data := await process.stderr.read(4096):
                tail = (tail + data)[-2048:]
            return tail.decode(errors="replace")

        heartbeat = asyncio.create_task(pulse())
        errors = asyncio.create_task(drain_errors())
        async for line in process.stdout:
            prefix = settings["prefix"].encode()
            position = line.find(prefix)
            if position < 0:
                continue
            value = json.loads(line[position + len(prefix):])
            if value["type"] == "data":
                stream = value["stream"]
                if stream not in cursors or value["offset"] != cursors[stream]:
                    raise ValueError("Log cursor mismatch; preserved existing local output")
                data = base64.b64decode(value["data"], validate=True)
                with (record_dir / stream).open("ab") as output:
                    output.write(data)
                cursors[stream] += len(data)
                audit.update(record_id, **{stream + "_bytes": cursors[stream]}, synced_at=time.time())
            elif value["type"] == "status":
                complete = value.pop("complete")
                value.pop("type")
                audit.update(record_id, **value, synced_at=time.time(),
                    sync_state="complete" if complete else "syncing",
                    status="finished" if complete else "running" if value.get("running") else "unknown")
            else:
                raise ValueError(value.get("message", "Log follower error"))
        await process.wait()
        diagnostic = await errors
        current = local_info(executor, handle["job_id"])
        if current.get("sync_state") != "complete":
            audit.update(record_id, sync_state="disconnected", running=None, execution_state="unknown",
                         sync_error=diagnostic or "Log channel closed; task completion is unconfirmed")
    except asyncio.CancelledError:
        if record_id:
            try:
                current = local_info(executor, handle["job_id"])
                if current.get("sync_state") != "complete":
                    audit.update(record_id, sync_state="disconnected", running=None,
                                 execution_state="unknown",
                                 sync_error="Local collection stopped; remote job was not cancelled")
            except (OSError, ValueError):
                pass
        raise
    except Exception as exc:
        if record_id:
            try:
                current = local_info(executor, handle["job_id"])
                if current.get("sync_state") != "complete":
                    audit.update(record_id, sync_state="disconnected", running=None,
                                 execution_state="unknown", sync_error=str(exc))
            except (OSError, ValueError):
                # A damaged/unwritable local record still remains non-complete;
                # never promote it merely because its follower failed.
                pass
    finally:
        for task in (heartbeat, errors):
            if task is not None:
                task.cancel()
        await asyncio.gather(*(task for task in (heartbeat, errors) if task is not None), return_exceptions=True)
        if process is not None:
            if process.stdin:
                process.stdin.close()  # EOF ends only the read-only follower.
            if process.returncode is None:
                try:
                    await asyncio.wait_for(process.wait(), 2)
                except asyncio.TimeoutError:
                    process.kill()  # Only this client, never the master/job.
                    await process.wait()
        if lock is not None:
            os.close(lock)
