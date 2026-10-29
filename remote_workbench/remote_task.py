"""Standalone remote task supervisor. No preload hooks or patched spawn APIs.

This source is also sent to remote Python; keep it stdlib-only and Python 3.10+.
The controller and supervisor stay outside the workload process group.
"""
import json
import os
from pathlib import Path
import re
import select
import signal
import subprocess
import sys
import tempfile
import threading
import time

if "login_spec" not in globals():
    from remote_workbench.remote_shell import login_spec


def machine_identity():
    value = {"node": os.uname().nodename, "boot_id": None}
    try:
        value["boot_id"] = Path("/proc/sys/kernel/random/boot_id").read_text().strip() or None
    except OSError:
        pass
    return value


def process_start(pid):
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return None
    if Path("/proc").is_dir():
        try:
            data = Path(f"/proc/{pid}/stat").read_text()
            fields = data[data.rindex(")") + 2:].split()
            return "proc:" + fields[19] if fields[0] != "Z" else None
        except (OSError, ValueError, IndexError):
            return None
    try:
        result = subprocess.run(["ps", "-p", str(pid), "-o", "lstart="],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=2)
        return "ps:" + result.stdout.decode().strip() if result.returncode == 0 and result.stdout.strip() else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def job_liveness(origin):
    current = machine_identity()
    detail = {"origin_node": origin.get("node"), "observed_node": current["node"]}
    if not isinstance(origin.get("node"), str) or not isinstance(origin.get("start_token"), str) or not origin["start_token"]:
        return None, dict(detail, state_reason="identity_unavailable")
    if origin["node"] != current["node"]:
        return None, dict(detail, state_reason="different_node")
    if origin.get("boot_id") and origin["boot_id"] != current["boot_id"]:
        return None, dict(detail, state_reason="different_boot")
    if origin["start_token"].startswith("proc:") and not origin.get("boot_id"):
        return None, dict(detail, state_reason="boot_identity_unavailable")
    if process_start(origin.get("pid")) != origin["start_token"]:
        return None, dict(detail, state_reason="process_unconfirmed")
    return True, {}


def verify_job_origin(origin, status, *, job_id, remote_state_dir,
                      expected_nonce, expected_launch_cwd, exit_code,
                      expected_identity=None, expected_actual_cwd=None):
    """Validate a persisted job identity before reading, following or stopping it.

    A terminal supervisor PID is expected to have exited, so terminal records
    are checked against the immutable origin/status pair and current node/boot
    identity instead of requiring that PID to remain live.
    """
    if not isinstance(origin, dict) or not isinstance(status, dict):
        return False, "origin_record_incomplete", None
    required = ("node", "pid", "start_token", "task_id", "task_dir")
    if expected_nonce is not None:
        required += ("origin_nonce", "launch_cwd", "actual_cwd")
    if any(origin.get(key) in (None, "") for key in required):
        return False, "origin_record_incomplete", None
    if (not isinstance(origin.get("pid"), int) or isinstance(origin.get("pid"), bool)
            or origin["pid"] <= 0 or not isinstance(origin.get("node"), str)
            or not isinstance(origin.get("start_token"), str)):
        return False, "origin_record_incomplete", None
    if origin.get("version") != 2:
        return False, "origin_version_unconfirmed", None
    if expected_nonce is not None and origin.get("origin_nonce") != expected_nonce:
        return False, "origin_nonce_mismatch", None
    if origin.get("task_id") != job_id:
        return False, "task_id_mismatch", None
    expected_dir = os.path.normpath(os.path.join(remote_state_dir, "jobs", job_id))
    if os.path.normpath(origin.get("task_dir", "")) != expected_dir:
        return False, "task_dir_mismatch", None
    if expected_launch_cwd is not None and origin.get("launch_cwd") != expected_launch_cwd:
        return False, "launch_cwd_mismatch", None
    if "actual_cwd" in origin and (not isinstance(origin.get("actual_cwd"), str) or
                                     not origin["actual_cwd"].startswith("/")):
        return False, "actual_cwd_unavailable", None
    if expected_actual_cwd is not None and origin.get("actual_cwd") != expected_actual_cwd:
        return False, "actual_cwd_mismatch", None
    identity_fields = ("node", "boot_id", "pid", "start_token", "task_id", "task_dir", "version")
    if "origin_nonce" in origin:
        identity_fields += ("origin_nonce",)
    if "launch_cwd" in origin:
        identity_fields += ("launch_cwd",)
    if "actual_cwd" in origin:
        identity_fields += ("actual_cwd",)
    for key in identity_fields:
        if status.get(key) != origin.get(key):
            return False, "status_origin_mismatch", None
    if expected_identity is not None:
        for key in ("node", "boot_id", "pid", "start_token", "task_id", "task_dir", "version"):
            if key in expected_identity and origin.get(key) != expected_identity.get(key):
                return False, "saved_process_identity_mismatch", None

    current = machine_identity()
    if current.get("node") != origin.get("node"):
        return False, "different_node", None
    origin_boot = origin.get("boot_id")
    current_boot = current.get("boot_id")
    if origin_boot and current_boot and origin_boot != current_boot:
        return False, "different_boot", None
    if (origin.get("start_token", "").startswith("proc:") and
            (not origin_boot or not current_boot)):
        return False, "boot_identity_unavailable", None

    finished = status.get("state") == "finished"
    if finished:
        if exit_code is None or status.get("exit_code") != exit_code:
            return False, "terminal_record_incomplete", None
        return True, None, False
    if exit_code is not None:
        return False, "terminal_record_incomplete", None
    if status.get("state") != "running":
        return False, "status_state_unconfirmed", None
    running, detail = job_liveness(origin)
    if running is not True:
        return False, detail.get("state_reason", "process_unconfirmed"), None
    return True, None, True


def task_state_root():
    default = Path.home() / ".cache" / "remote-workbench-ssh"
    return Path(os.environ.get("REMOTE_WORKBENCH_REMOTE_STATE_DIR", str(default))).expanduser().absolute()


def save_task_json(path, value):
    temporary = path.with_suffix(".tmp")
    with open(temporary, "w", opener=lambda p, f: os.open(p, f, 0o600)) as output:
        json.dump(value, output)
    os.replace(temporary, path)


def child_exited(process, completion=None):
    # Keep the leader unreaped until cleanup has finished. Its PID/PGID cannot
    # be reused while a zombie is still our child. Never poll()/wait() early.
    if completion is not None:
        return completion.exists()
    return os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is not None


def signal_workload_group(process, number, completion=None):
    try:
        os.killpg(process.pid, number)
    except ProcessLookupError:
        pass
    except PermissionError:
        # macOS returns EPERM for a group containing only our retained zombie.
        # Ignore it only after positively excluding any live group member.
        if not child_exited(process, completion):
            raise
        result = subprocess.run(["ps", "-axo", "pid=,pgid=,stat="], capture_output=True, timeout=2, check=True)
        for line in result.stdout.decode().splitlines():
            fields = line.split()
            if len(fields) == 3 and fields[1] == str(process.pid) and not fields[2].startswith("Z"):
                raise PermissionError("Could not signal a live workload group member")


def cleanup_workload(process, grace, completion=None):
    """TERM, then KILL of this owned group; this is not a process-count cap."""
    signal_workload_group(process, signal.SIGTERM, completion)
    end = time.monotonic() + grace
    while time.monotonic() < end:
        if child_exited(process, completion):
            break
        time.sleep(0.05)
    signal_workload_group(process, signal.SIGKILL, completion)
    code = process.wait(timeout=2)
    if completion is not None and completion.exists():
        code = int(completion.read_text())
    return code, {"cleanup_state": "process_group_signalled", "tree_confirmed": False,
                  "cleanup_scope": "process_group", "hard_process_limit": False}


def read_task_input(length, deadline):
    data = bytearray()
    while len(data) < length:
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not select.select([0], [], [], remaining)[0]:
            raise ValueError("Timed out receiving task input")
        chunk = os.read(0, min(65536, length - len(data)))
        if not chunk:
            raise ValueError("Controller disconnected before task input completed")
        data.extend(chunk)
    return bytes(data)


def forward_task_output(reader, name, root, marker):
    """Detached logs retain the same startup/output separation as foreground."""
    pending = b""
    started = False
    output = getattr(sys, name).buffer
    with open(root / ("startup_" + name + ".log"), "wb",
              opener=lambda p, f: os.open(p, f, 0o600)) as startup:
        for chunk in iter(lambda: reader.read1(65536), b""):
            if started:
                output.write(chunk)
                output.flush()
                continue
            pending += chunk
            offset = pending.find(marker)
            if offset >= 0:
                startup.write(pending[:offset])
                output.write(pending[offset + len(marker):])
                output.flush()
                pending = b""
                started = True
            elif len(pending) > 65536:
                startup.write(pending[:-1024])
                pending = pending[-1024:]
        if not started:
            startup.flush()
            output.write((root / ("startup_" + name + ".log")).read_bytes() + pending)
            output.flush()


def batch_progress(root, nonce, count=None):
    """Read only bounded structured progress, never infer step results from stdout."""
    try:
        path = root / 'batch-progress.json'
        if path.is_symlink() or path.stat().st_size > 131072:
            raise ValueError('Invalid batch progress')
        with path.open('rb') as source:
            data = source.read(131073)
        value = json.loads(data)
        if not isinstance(value, dict):
            raise ValueError('Invalid batch progress')
        rows = value['steps']
        if (len(data) > 131072 or value.get('nonce') != nonce or value.get('version') != 1
                or type(value.get('complete')) is not bool or not isinstance(rows, list) or not 1 <= len(rows) <= 32
                or (count is not None and len(rows) != count)):
            raise ValueError('Invalid batch identity')
        result = []
        for index, row in enumerate(rows):
            if not isinstance(row, dict):
                raise ValueError('Invalid batch step')
            state, code = row['state'], row['exit_code']
            if (row.get('index') != index or state not in {'not_started','running','finished','not_run'}
                    or (code is not None and type(code) is not int)
                    or (state == 'finished') != (code is not None)
                    or (value['complete'] and state in {'not_started', 'running'})
                    or type(row.get('elapsed_ms')) is not int or row['elapsed_ms'] < 0):
                raise ValueError('Invalid batch step')
            item = {k: row[k] for k in ('index', 'state', 'exit_code', 'elapsed_ms')}
            item['state'] = 'unknown' if state == 'running' else 'not_run' if state == 'not_started' else state
            if type(row.get('signal')) is int:
                item['signal'] = row['signal']
            if isinstance(row.get('error'), str):
                item['error'] = row['error']
            if isinstance(item.get('error'), str) and len(item['error']) > 160:
                item['error'] = item['error'][:160]
                item['error_truncated'] = True
            result.append(item)
        return {'batch_state': 'finished' if value['complete'] else 'unknown', 'batch': result}
    except (OSError, ValueError, KeyError, TypeError):
        return {'batch_state': 'unknown', 'batch': [], 'batch_reason': 'progress_unavailable'}


def supervise_task(settings):
    """Run one workload, with an independent deadline and foreground lease."""
    policy = settings["policy"]
    timeout = settings["timeout"]
    if timeout <= 0:
        raise ValueError("Task deadline must be positive")
    retains_zombie = all(hasattr(os, name) for name in ("waitid", "WNOWAIT", "WEXITED"))
    task_id = settings["task_id"]
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", task_id):
        raise ValueError("Invalid task ID")
    root = Path(settings["task_dir"])
    root.mkdir(parents=True, mode=0o700, exist_ok=settings.get("directory_exists", False))
    foreground = not settings.get("detached", False)
    marker = ("\x1eREMOTE_WORKBENCH_TASK_" + task_id + ":").encode()

    def publish(value):
        save_task_json(root / "status.json", value)
        if foreground:
            try:
                os.write(2, marker + json.dumps(value).encode() + b"\x1f\n")
            except OSError:
                pass

    origin = dict(machine_identity(), version=2, pid=os.getpid(), start_token=process_start(os.getpid()),
                  task_id=task_id, task_dir=str(root), backend="process-group",
                  timeout_seconds=timeout, origin_nonce=settings.get("origin_nonce"),
                  launch_cwd=settings.get("launch_cwd", settings.get("cwd")),
                  actual_cwd=settings.get("actual_cwd", settings.get("cwd")))
    save_task_json(root / "origin.json", origin)
    process = None
    completion = None if retains_zombie else root / "workload_exit_code"
    reason = "finished"
    code = 126
    details = {"cleanup_state": "not_started", "tree_confirmed": True}
    received_signal = []
    threads = []
    old_handlers = {}
    batch_count = None
    # Wake on child completion instead of adding a polling interval to every
    # short operation. Keep the leader unreaped until owned-group cleanup.
    wake_read, wake_write = os.pipe()
    os.set_blocking(wake_write, False)
    def wake_parent(_number, _frame):
        try:
            os.write(wake_write, b'E')
        except OSError:
            pass
    old_handlers[signal.SIGCHLD] = signal.signal(signal.SIGCHLD, wake_parent)
    for number in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        old_handlers[number] = signal.signal(number, lambda n, f: received_signal.append(n))
    deadline = time.monotonic() + timeout
    try:
        if not policy["execution_enabled"]:
            raise ValueError("Remote execution is disabled")
        if ('.remote-workbench-sessions' in
                Path(settings['cwd']).expanduser().resolve().parts):
            raise ValueError('Managed project copies are not direct working directories; select the original source')
        data = read_task_input(settings.get("stdin_bytes", 0), deadline) if foreground else b""
        command = settings['command']
        if settings.get('batch'):
            # The argv JSON arrives on stdin, never as model-authored shell source
            # or a large SSH argv. Only this fixed runner is executable code.
            batch_spec = json.loads(data)
            batch_count = len(batch_spec['commands'])
            save_task_json(root / 'batch-input.json', batch_spec)
            source = globals().get('BATCH_RUNNER_SOURCE')
            if source is None:
                source = Path(__file__).with_name('batch_runner.py').read_text()
            command = python_command(source + '\nraise SystemExit(run_batch(json.load(open(' +
                repr(str(root / 'batch-input.json')) + ')), ' + repr(str(root / 'batch-progress.json')) + ', ' + repr(task_id) + '))\n')
        with tempfile.TemporaryFile() as input_file:
            input_file.write(b'' if settings.get('batch') else data)
            input_file.seek(0)
            spec = login_spec(command, settings["cwd"], preamble=settings.get("preamble", ""),
                              markers=settings.get("markers"), trace=settings.get("trace", False),
                              environment=settings.get("environment"))
            streams = {name: subprocess.PIPE for name in ("stdout", "stderr")} if not foreground else {}
            child_environment = dict(os.environ, **spec["env_updates"])
            if completion is None:
                # Popen's explicit executable preserves login argv[0] directly;
                # an intermediate Python exec launcher adds no supervision.
                process = subprocess.Popen(spec["argv"], executable=spec["shell"], env=child_environment,
                                           stdin=input_file, start_new_session=True, **streams)
            else:
                # macOS without waitid/WNOWAIT still needs a live group leader.
                # Only this fixed guardian receives the notification descriptor;
                # the actual workload closes it and retains its native shell.
                guardian = "import os,signal,sys,time\n"
                guardian += "for n in (signal.SIGTERM,signal.SIGHUP,signal.SIGINT): signal.signal(n,signal.SIG_IGN)\n"
                guardian += "try:\n pid=os.posix_spawn(" + repr(spec["shell"]) + "," + repr(spec["argv"]) + ",os.environ,setsigdef=(signal.SIGTERM,signal.SIGHUP,signal.SIGINT),file_actions=[(os.POSIX_SPAWN_CLOSE," + repr(wake_write) + ")])\n"
                guardian += " with open(" + repr(str(root / "workload_pid.tmp")) + ",'w') as f: f.write(str(pid))\n"
                guardian += " os.replace(" + repr(str(root / "workload_pid.tmp")) + "," + repr(str(root / "workload_pid")) + ")\n code=os.waitstatus_to_exitcode(os.waitpid(pid,0)[1])\n"
                guardian += "except OSError as exc:\n print(str(exc),file=sys.stderr,flush=True)\n code=127\n"
                guardian += "with open(" + repr(str(completion) + ".tmp") + ",'w') as f: f.write(str(code))\n"
                guardian += "os.replace(" + repr(str(completion) + ".tmp") + "," + repr(str(completion)) + ")\n"
                guardian += "os.write(" + repr(wake_write) + ",b'E')\n"
                guardian += "while True: time.sleep(3600)\n"
                process = subprocess.Popen([sys.executable, "-c", guardian], stdin=input_file,
                                           start_new_session=True, env=child_environment, pass_fds=(wake_write,), **streams)
        if not foreground:
            for name in ("stdout", "stderr"):
                thread = threading.Thread(target=forward_task_output,
                    args=(getattr(process, name), name, root, settings["markers"][name].encode()), daemon=True)
                thread.start()
                threads.append(thread)
        origin.update(workload_pid=process.pid if retains_zombie else None, pgid=process.pid)
        save_task_json(root / "origin.json", origin)
        publish(dict(origin, state="running"))
        last_heartbeat = time.monotonic()
        while True:
            if completion is not None and origin["workload_pid"] is None and (root / "workload_pid").exists():
                origin["workload_pid"] = int((root / "workload_pid").read_text())
                save_task_json(root / "origin.json", origin)
                publish(dict(origin, state="running"))
            if received_signal:
                reason = "supervisor_signal"
                break
            if (root / "cancel").exists():
                reason = "cancelled"
                break
            if time.monotonic() >= deadline:
                reason = "timeout"
                break
            if foreground:
                if select.select([0], [], [], 0)[0]:
                    message = os.read(0, 4096)
                    if not message:
                        reason = "controller_disconnected"
                        break
                    if b"C" in message:
                        reason = "cancelled"
                        break
                    last_heartbeat = time.monotonic()
                if time.monotonic() - last_heartbeat > 5:
                    reason = "controller_lease_expired"
                    break
            if child_exited(process, completion):
                break
            watched = [wake_read, 0] if foreground else [wake_read]
            ready, _, _ = select.select(watched, [], [], 0.05)
            if wake_read in ready:
                os.read(wake_read, 4096)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        reason = "launch_failed"
        print(str(exc), file=sys.stderr, flush=True)
        details["message"] = str(exc)
    finally:
        if process is not None:
            try:
                code, cleaned = cleanup_workload(process, policy["terminate_grace_seconds"], completion)
                details.update(cleaned)
            except (OSError, ValueError, subprocess.SubprocessError) as exc:
                details.update(cleanup_state="unconfirmed", tree_confirmed=False, message=str(exc))
                # Do not reap a still-running workload and call it cancelled.
        for thread in threads:
            thread.join(timeout=2)
        if any(thread.is_alive() for thread in threads):
            details.update(cleanup_state="unconfirmed", tree_confirmed=False,
                           message="A descendant still holds an output pipe open")
        for number, handler in old_handlers.items():
            signal.signal(number, handler)
        os.close(wake_read)
        os.close(wake_write)
        if reason == "timeout":
            code = 124
        elif reason in {"cancelled", "controller_disconnected", "controller_lease_expired", "supervisor_signal"}:
            code = 130
        elif reason == "launch_failed":
            code = 126
        code = code if code >= 0 else 128 - code
        final = dict(origin, state="finished", reason=reason, exit_code=code, **details)
        if settings.get('batch'):
            final.update(batch_progress(root, task_id, batch_count))
        publish(final)
        with open(root / "exit_code.tmp", "w", opener=lambda p, f: os.open(p, f, 0o600)) as output:
            output.write(str(code))
        os.replace(root / "exit_code.tmp", root / "exit_code")
    return code


def cancel_task(root, wait_seconds=0, *, expected_nonce=None,
               expected_launch_cwd=None, expected_remote_state_dir=None, task_id=None,
               expected_identity=None, expected_actual_cwd=None):
    """Independent control request. Never kill a PID taken from an old file."""
    root = Path(root)
    expected_origin = expected_nonce is not None or expected_identity is not None
    directory_fd = None
    directory_identity = None

    def read_expected():
        if root.is_symlink() or any((root / name).is_symlink()
                                    for name in ("origin.json", "status.json", "exit_code")):
            return None, None, "job_identity_path_is_symlink"
        try:
            current_origin = json.loads((root / "origin.json").read_text())
            current_status = json.loads((root / "status.json").read_text())
            exit_path = root / "exit_code"
            code = int(exit_path.read_text()) if exit_path.exists() else None
            valid, reason, _ = verify_job_origin(
                current_origin, current_status, job_id=task_id,
                remote_state_dir=expected_remote_state_dir,
                expected_nonce=expected_nonce, expected_launch_cwd=expected_launch_cwd,
                exit_code=code, expected_identity=expected_identity,
                expected_actual_cwd=expected_actual_cwd)
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return None, None, "origin_record_incomplete"
        if not valid:
            return None, None, reason
        return current_origin, current_status, None

    if expected_origin:
        if (expected_remote_state_dir is None or
                str(task_state_root()) != str(Path(expected_remote_state_dir).expanduser().absolute())):
            return {"cancel_requested": False, "execution_state": "unknown",
                    "state_reason": "remote_state_dir_mismatch"}
        origin, status, reason = read_expected()
        if reason:
            return {"cancel_requested": False, "execution_state": "unknown", "state_reason": reason}
        try:
            directory_fd = os.open(root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) |
                                   getattr(os, "O_NOFOLLOW", 0))
            opened = os.fstat(directory_fd)
            current = os.stat(root, follow_symlinks=False)
            directory_identity = (opened.st_dev, opened.st_ino)
            if directory_identity != (current.st_dev, current.st_ino):
                os.close(directory_fd)
                directory_fd = None
                return {"cancel_requested": False, "execution_state": "unknown",
                        "state_reason": "job_identity_path_changed"}
        except OSError as exc:
            if directory_fd is not None:
                os.close(directory_fd)
                directory_fd = None
            return {"cancel_requested": False, "execution_state": "unknown",
                    "state_reason": "job_directory_unavailable", "message": str(exc)}
    else:
        try:
            status = json.loads((root / "status.json").read_text())
        except FileNotFoundError:
            status = None
        origin = json.loads((root / "origin.json").read_text())
        if not status or status.get("state") != "finished":
            running, detail = job_liveness(origin)
            if not running or origin.get("version") != 2:
                return {"cancel_requested": False, "execution_state": "unknown", **detail}

    try:
        if status and status.get("state") == "finished":
            return dict(status, cancel_requested=False,
                        execution_state="unknown" if status.get("cleanup_state") == "unconfirmed" else "finished")
        # The supervisor outside the workload scope performs TERM -> KILL itself.
        if directory_fd is not None:
            fd = os.open("cancel", os.O_CREAT | os.O_EXCL | os.O_WRONLY |
                         getattr(os, "O_NOFOLLOW", 0), 0o600, dir_fd=directory_fd)
        else:
            fd = os.open(root / "cancel", os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
        end = time.monotonic() + wait_seconds
        while time.monotonic() < end:
            if expected_origin:
                try:
                    current = os.stat(root, follow_symlinks=False)
                except OSError:
                    return {"cancel_requested": True, "execution_state": "unknown",
                            "state_reason": "job_identity_path_changed"}
                if (current.st_dev, current.st_ino) != directory_identity:
                    return {"cancel_requested": True, "execution_state": "unknown",
                            "state_reason": "job_identity_path_changed"}
                _, status, reason = read_expected()
                if reason:
                    return {"cancel_requested": True, "execution_state": "unknown",
                            "state_reason": reason}
            else:
                try:
                    status = json.loads((root / "status.json").read_text())
                except FileNotFoundError:
                    status = None
            if status and status.get("state") == "finished":
                return dict(status, cancel_requested=True,
                            execution_state="unknown" if status.get("cleanup_state") == "unconfirmed" else "finished")
            time.sleep(0.05)
        return {"cancel_requested": True, "execution_state": "cancellation_requested",
                "node": origin["node"], "pid": origin["pid"], "pgid": origin.get("pgid")}
    except FileExistsError:
        return {"cancel_requested": True, "execution_state": "cancellation_requested",
                "node": origin["node"], "pid": origin["pid"], "pgid": origin.get("pgid")}
    finally:
        if directory_fd is not None:
            os.close(directory_fd)
