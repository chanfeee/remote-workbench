"""Standalone stdlib-only worker, sent to remote Python through SSH.

Locks coordinate enhanced MCP writers, not arbitrary editors. Hash/stat checks
detect stale reads; no claim of a transaction against uncooperative processes.
"""
import base64
import difflib
import fcntl
import hashlib
import json
import os
import re
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
from contextlib import contextmanager

if "login_spec" not in globals():
    from remote_workbench.remote_shell import login_spec
if "prepared_file_patch" not in globals():
    from remote_workbench.native_patch import compile_native_patch, prepared_file_patch


def state_root():
    default = Path.home() / ".cache" / "remote-workbench-ssh"
    return Path(os.environ.get("REMOTE_WORKBENCH_REMOTE_STATE_DIR", str(default))).expanduser().absolute()


@contextmanager
def lock(path):
    # One writer lock per remote user also coordinates Git patches with file
    # replacements. Keep bookkeeping outside repositories.
    base = state_root() / "locks"
    base.mkdir(parents=True, exist_ok=True, mode=0o700)
    lockpath = base / "writer.lock"
    fd = os.open(lockpath, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def atomic_write(path, data, previous):
    info = path.stat() if path.exists() else None
    if path.is_symlink():
        raise ValueError("Refusing to replace a symbolic link")
    fd, name = tempfile.mkstemp(prefix=".remote-workbench-write-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(data)
            if info:
                # Preserve ownership when allowed; fail rather than silently change it.
                if (os.fstat(output.fileno()).st_uid, os.fstat(output.fileno()).st_gid) != (info.st_uid, info.st_gid):
                    os.fchown(output.fileno(), info.st_uid, info.st_gid)
                os.fchmod(output.fileno(), stat.S_IMODE(info.st_mode))
            output.flush()
            os.fsync(output.fileno())
        now = path.read_bytes() if path.exists() else None
        if now != previous:
            raise ValueError("Conflict: file changed before replacement")
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def edit(request):
    path = Path(request["path"]).expanduser().absolute()
    if request.get("create_dirs"):
        path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise ValueError("Refusing to edit a symbolic link")
    with lock(path):
        original = path.read_bytes() if path.exists() else None
        if original is not None and len(original) > request.get("max_file_size", 10485760):
            raise ValueError("File exceeds configured size limit")
        old = original or b""
        expected = request.get("expected_sha256")
        if expected is not None and sha(old) != expected:
            return {"error": "conflict", "actual_sha256": sha(old)}
        before = old.decode("utf-8")
        text = before
        op = request["op"]
        if op == "replace":
            if original is None:
                raise FileNotFoundError(str(path))
            # Sequential edits are explicit; validate all before writing anything.
            for item in request["edits"]:
                needle = item["old"]
                if not needle:
                    raise ValueError("old must not be empty")
                count = text.count(needle)
                if count != 1:
                    return {"error": "not_found" if count == 0 else "ambiguous", "matches": count, "path": str(path)}
                text = text.replace(needle, item["new"], 1)
        elif op == "lines":
            if original is None:
                raise FileNotFoundError(str(path))
            lines = text.splitlines(keepends=True)
            start, end = request["start"], request["end"]
            if start < 0 or end < start or end > len(lines):
                raise ValueError("Line range outside file")
            if "expected_old" in request and "".join(lines[start:end]) != request["expected_old"]:
                return {"error": "conflict", "message": "Line contents changed"}
            text = "".join(lines[:start]) + request["content"] + "".join(lines[end:])
        elif op == "append":
            text += request["content"]
        elif op == "write":
            text = request["content"]
        else:
            raise ValueError("Unknown edit operation")
        after = text.encode("utf-8")
        if len(after) > request.get("max_file_size", 10485760):
            raise ValueError("Result exceeds configured size limit")
        diff_lines = difflib.unified_diff(before.splitlines(keepends=True), text.splitlines(keepends=True), fromfile=str(path), tofile=str(path), n=3)
        patch = "".join(line if line.endswith("\n") else line + "\n\\ No newline at end of file\n" for line in diff_lines)
        if not request.get("check_only") and (original is None or old != after):
            atomic_write(path, after, original)
        return {"success": True, "path": str(path), "check_only": bool(request.get("check_only")),
                "changed": old != after or original is None, "before_sha256": sha(old), "after_sha256": sha(after), "diff": patch}


def apply_patch(request):
    patch = request["patch"].encode("utf-8")
    prepared = request.get('prepared_contents')
    expected_repo = request.get('expected_repository')
    if prepared is not None:
        # Snapshot edits already identify their exact root and guarded files.
        # Their content/diff validation and atomic writes need no Git process.
        if not isinstance(expected_repo, str) or not expected_repo.startswith('/'):
            raise ValueError('Prepared edits require an absolute edit root')
        repo = Path(expected_repo)
        if repo.resolve() != repo or not repo.is_dir():
            raise ValueError('Prepared edit root changed or is unavailable')
    else:
        # Git apply also operates on ordinary directories, without git init.
        # Preserve repository-relative paths for existing unified/context calls.
        try:
            top = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True)
        except FileNotFoundError:
            return {'error': 'git_unavailable',
                    'message': 'Patch text needs the Git executable, not a repository; use read_file(for_edit=true) and edits for Git-free editing'}
        repo = Path(os.fsdecode(top.stdout).strip()) if top.returncode == 0 else Path.cwd()
    os.chdir(repo)
    with lock(repo / ".remote-workbench-patch"):
        expected_repo = request.get('expected_repository')
        if expected_repo is not None and str(repo) != expected_repo:
            return {'error': 'edit_target_mismatch', 'message': 'Prepared edit belongs to another edit root'}
        guards = request.get('expected_files')
        originals = {}
        if guards is not None:
            if not isinstance(guards, dict) or not 1 <= len(guards) <= 100:
                raise ValueError('Invalid prepared edit guards')
            for name, expected in guards.items():
                path = Path(name)
                try:
                    relative = path.relative_to(repo)
                except ValueError:
                    raise ValueError('Prepared edit path is outside the edit root')
                if (not relative.parts or '..' in relative.parts or any(p.casefold() == '.git' for p in path.parts)
                        or path.resolve() != path or not re.fullmatch('[a-f0-9]{64}', str(expected))):
                    raise ValueError('Unsafe prepared edit guard')
                fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                with os.fdopen(fd, 'rb') as source:
                    info = os.fstat(source.fileno())
                    maximum = request.get('max_file_size', 10485760)
                    if not stat.S_ISREG(info.st_mode) or info.st_size > maximum:
                        raise ValueError('Prepared edit input exceeds the file size limit')
                    current = source.read(maximum + 1)
                if len(current) > maximum or sha(current) != expected:
                    return {'error': 'conflict', 'path': name, 'actual_sha256': sha(current),
                            'message': 'File changed since the edit snapshot; read it again before editing'}
                originals[name] = current
        prepared = request.get('prepared_contents')
        if prepared is not None:
            if (not isinstance(prepared, dict) or not guards or set(prepared) != set(guards)
                    or any(not isinstance(v, str) or len(v.encode()) > request.get('max_file_size', 10485760)
                           for v in prepared.values())):
                raise ValueError('Invalid prepared file contents')
            if prepared_file_patch(originals, prepared, repo).encode() != patch:
                raise ValueError('Prepared contents differ from the reviewed patch')
        if not patch:
            return {'success': True, 'check_only': bool(request.get('check_only')),
                    'summary': '', 'patch_sha256': sha(patch), 'changed': False}
        native = request["patch"].startswith("*** Begin Patch")
        if native:
            patch = compile_native_patch(request["patch"], repo, request.get("max_file_size", 10485760)).encode("utf-8")
            if not patch:
                return {"success": True, "check_only": bool(request.get("check_only")),
                        "summary": "", "patch_sha256": sha(patch), "normalized_patch": ""}
        if prepared is not None:
            # All guards and the generated patch were checked before any write.
            # Record this edit's line changes without a repository or Git scan.
            rows = []
            for name, content in prepared.items():
                diff = list(difflib.unified_diff(originals[name].decode('utf-8').splitlines(keepends=True),
                                                 content.splitlines(keepends=True)))
                added = sum(line.startswith('+') for line in diff[2:])
                removed = sum(line.startswith('-') for line in diff[2:])
                if diff:
                    rows.append(str(added) + '\t' + str(removed) + '\t' + str(Path(name).relative_to(repo)))
            summary_text = '\n'.join(rows) + ('\n' if rows else '')
            if not request.get('check_only'):
                applied = []
                for name, content in prepared.items():
                    after = content.encode('utf-8')
                    if after != originals[name]:
                        try:
                            atomic_write(Path(name), after, originals[name])
                        except (OSError, ValueError) as exc:
                            return {'error': 'partial_apply' if applied else 'apply_failed', 'message': str(exc),
                                    'applied_paths': applied,
                                    'pending_paths': [p for p in prepared if p not in applied]}
                        applied.append(name)
        else:
            # Git's normal path/context protections apply in both worktrees and
            # plain directories. No index, repository creation or project scan.
            check = subprocess.run(["git", "apply", "--check", "--whitespace=nowarn", "-"], input=patch, capture_output=True)
            if check.returncode:
                return {"error": "patch_conflict", "message": check.stderr.decode("utf-8", "replace")}
            summary = subprocess.run(["git", "apply", "--numstat", "-"], input=patch, capture_output=True, check=True)
            summary_text = summary.stdout.decode('utf-8', 'replace')
            if not request.get('check_only'):
                result = subprocess.run(["git", "apply", "--whitespace=nowarn", "-"], input=patch, capture_output=True)
                if result.returncode:
                    return {"error": "apply_failed", "message": result.stderr.decode("utf-8", "replace")}
        result = {"success": True, "check_only": bool(request.get("check_only")), "summary": summary_text,
                  "patch_sha256": sha(patch), "changed": bool(summary_text)}
        if native:
            result["normalized_patch"] = patch.decode("utf-8")
        return result


def read_binary(request):
    path = Path(request["path"]).expanduser()
    maximum = request.get("max_file_size", 10485760)
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as source:
        before = os.fstat(source.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise ValueError("Image source must be a regular file")
        if before.st_size > maximum:
            raise ValueError("File exceeds configured size limit")
        data = source.read(maximum + 1)
        after = os.fstat(source.fileno())
    if len(data) > maximum:
        raise ValueError("File exceeds configured size limit")
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise ValueError("File changed while being read; retry after the writer finishes")
    return {"data_base64": base64.b64encode(data).decode("ascii"),
            "size": len(data), "sha256": sha(data)}


def read_lines(request):
    path = Path(request["path"]).expanduser()
    if request.get('for_edit'):
        path = path.absolute()
        if any(part.casefold() == '.git' for part in path.parts):
            return {'error': 'protected_git_path', 'path': str(path),
                    'message': 'Git metadata inside .git cannot be selected for editing.'}
        for component in [*reversed(path.parents), path]:
            if component.is_symlink():
                return {'error': 'edit_symlink_path', 'path': str(path),
                        'symlink_component': str(component),
                        'message': 'Edit snapshots do not follow symbolic links; select the actual file path explicitly.'}
        if path.resolve() != path:
            return {'error': 'edit_path_not_canonical', 'path': str(path),
                    'resolved_path': str(path.resolve()),
                    'message': 'Select the canonical file path before requesting an edit snapshot.'}
        try:
            info = path.stat()
        except FileNotFoundError:
            return {'error': 'file_not_found', 'path': str(path),
                    'message': 'File does not exist. Edit snapshots require an existing file; create a new file with workspace_tools("apply_patch") Add File or workspace_tools("write_file").'}
        if not stat.S_ISREG(info.st_mode):
            return {'error': 'not_regular_file', 'path': str(path),
                    'message': 'Edit snapshots require a regular file; the selected path is a directory or another file type.'}
    maximum = request.get("max_file_size", 10485760)
    if path.stat().st_size > maximum:
        raise ValueError("File exceeds configured size limit")
    # Hash and selected lines refer to exactly the same bounded snapshot.
    with path.open("rb") as source:
        snapshot = source.read(maximum + 1)
    if len(snapshot) > maximum:
        raise ValueError("File exceeds configured size limit")
    lines = snapshot.decode("utf-8").splitlines(keepends=True)
    selected = []
    total = len(lines)
    used = 0
    truncated = False
    next_offset = request["offset"]
    for index in range(request["offset"], min(total, request["offset"] + request["limit"])):
        value = f"{index+1}\t{lines[index]}"
        size = len(value.encode("utf-8"))
        if used + size > request["budget"]:
            if not selected:
                return {"error": "line_exceeds_budget", "line": index+1, "required_bytes": size, "sha256": sha(snapshot)}
            truncated = True
            break
        selected.append(value)
        used += size
        next_offset = index + 1
    result = {"content": "".join(selected).rstrip("\n"), "total_lines": total,
            "sha256": sha(snapshot), "next_offset": next_offset,
            "truncated": truncated or total > next_offset}
    if request.get('for_edit'):
        # A workspace/edit root is a directory, not a repository. Files outside
        # the current cwd can still be explicitly selected by their full path.
        edit_root = Path.cwd()
        if not path.is_relative_to(edit_root):
            edit_root = path.parent
        result.update(_edit_data_base64=base64.b64encode(snapshot).decode('ascii'),
                      _edit_path=str(path), _edit_repository=str(edit_root))
    return result


def read_selected(request):
    """One bounded, explicit selection; never discover or scan a project."""
    selection = request.get('files')
    if (not isinstance(selection, list) or not 1 <= len(selection) <= 16
            or any(not isinstance(item, dict) or not isinstance(item.get('path'), str)
                   or not item['path'] or '\x00' in item['path']
                   or type(item.get('for_edit')) is not bool for item in selection)):
        raise ValueError('Select 1..16 explicit files')
    results, used = [], 0
    snapshot_budget = 4 * ((2 * request.get('max_file_size', 10485760) + 2) // 3)
    for item in selection:
        try:
            if '.remote-workbench-sessions' in Path(os.path.abspath(item['path'])).parts:
                raise ValueError('Select files from the actual directory, not a managed project copy')
            value = read_lines(dict(request, path=item['path'], for_edit=item['for_edit']))
            size = len(value.get('_edit_data_base64', ''))
            if used + size > snapshot_budget:
                value = {'error': 'snapshot_batch_budget', 'message': 'Read this file individually; selected snapshots exceed the batch budget'}
            else:
                used += size
        except (OSError, ValueError, UnicodeError) as exc:
            value = {'error': 'read_failed', 'message': str(exc)[:512]}
        results.append(value)
    return {'files': results}



# The same supervisor owns foreground and detached workload lifecycles.
if "supervise_task" not in globals():
    from remote_workbench.remote_task import machine_identity, process_start, job_liveness, supervise_task, verify_job_origin
if "verify_job_origin" not in globals():
    from remote_workbench.remote_task import verify_job_origin
if "TASK_SUPERVISOR_SOURCE" not in globals():
    TASK_SUPERVISOR_SOURCE = (Path(__file__).with_name("remote_shell.py").read_text() + "\n" +
                              Path(__file__).with_name("remote_task.py").read_text())


def job(request):
    import re
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", request["job_id"]):
        raise ValueError("Invalid job ID")
    base = state_root() / "jobs"
    path = base / request["job_id"]
    if request["op"] == "job_start":
        origin_nonce = request.get("origin_nonce")
        if origin_nonce is not None and (not isinstance(origin_nonce, str) or
                not re.fullmatch(r"[a-f0-9]{32}", origin_nonce)):
            raise ValueError("Invalid job origin nonce")
        launch_cwd = request.get("launch_cwd", os.getcwd())
        if not isinstance(launch_cwd, str) or not launch_cwd.startswith("/") or "\x00" in launch_cwd:
            raise ValueError("Invalid job launch cwd")
        base.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.mkdir(mode=0o700)  # Exclusive: never overwrite another job.
        runner = path / "runner.py"
        # A separate supervisor captures even an explicit `exit` in the user
        # command, while the command itself uses the same native shell as exec.
        prefix = "\x1eREMOTE_WORKBENCH_JOB_" + request["job_id"]
        markers = {name: prefix + "_" + name + "\x1f\n" for name in ("stdout", "stderr")}
        policy = request["_task_policy"]
        settings = dict(command=request["command"], cwd=os.getcwd(), actual_cwd=os.getcwd(),
                        launch_cwd=launch_cwd, task_id=request["job_id"],
                        task_dir=str(path), markers=markers, policy=policy,
                        timeout=request.get("timeout") or policy["job_timeout"],
                        detached=True, directory_exists=True, origin_nonce=origin_nonce)
        script = TASK_SUPERVISOR_SOURCE + "\nsettings = " + repr(settings) + "\nraise SystemExit(supervise_task(settings))\n"
        with open(runner, "w", opener=lambda p, f: os.open(p, f, 0o600)) as output:
            output.write(script)
        with open(path / "stdout.log", "wb", opener=lambda p, f: os.open(p, f, 0o600)) as out, open(path / "stderr.log", "wb", opener=lambda p, f: os.open(p, f, 0o600)) as err:
            process = subprocess.Popen([sys.executable, str(runner)], stdin=subprocess.DEVNULL,
                stdout=out, stderr=err, start_new_session=True)
        atomic_write(path / "pid", str(process.pid).encode(), None)
        # Record supervisor identity before returning a handle. Even a launch
        # failure leaves a finite-deadline supervisor, never an unowned command.
        import time
        deadline = time.monotonic() + 3
        while not (path / "status.json").exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        if not (path / "status.json").exists():
            return {"error": "job_start_unconfirmed", "job_id": request["job_id"], "pid": process.pid}
        status = json.loads((path / "status.json").read_text())
        return {"job_id": request["job_id"], "pid": process.pid, "job_dir": str(path),
                "node": status["node"], "pgid": status.get("pgid"), "remote_task": status}
    if not path.is_dir():
        return {"error": "job_not_found"}
    expected_nonce = request.get("expected_origin_nonce")
    expected_launch_cwd = request.get("expected_launch_cwd")
    expected_actual_cwd = request.get("expected_actual_cwd")
    expected_state_dir = request.get("expected_remote_state_dir")
    if expected_nonce is not None:
        if (not isinstance(expected_nonce, str) or
                not re.fullmatch(r"[a-f0-9]{32}", expected_nonce) or
                not isinstance(expected_launch_cwd, str) or
                not isinstance(expected_state_dir, str)):
            return {"error": "job_identity_unconfirmed", "job_id": request["job_id"],
                    "running": None, "execution_state": "unknown",
                    "state_reason": "recovery_descriptor_incomplete"}
        if str(state_root()) != str(Path(expected_state_dir).expanduser().absolute()):
            return {"error": "job_identity_unconfirmed", "job_id": request["job_id"],
                    "running": None, "execution_state": "unknown",
                    "state_reason": "remote_state_dir_mismatch"}
        if path.is_symlink() or any((path / name).is_symlink()
                                    for name in ("origin.json", "status.json", "exit_code",
                                                 "stdout.log", "stderr.log")):
            return {"error": "job_identity_unconfirmed", "job_id": request["job_id"],
                    "running": None, "execution_state": "unknown",
                    "state_reason": "job_identity_path_is_symlink"}
        try:
            origin = json.loads((path / "origin.json").read_text())
            status = json.loads((path / "status.json").read_text())
            exit_path = path / "exit_code"
            exit_code = int(exit_path.read_text()) if exit_path.exists() else None
            valid, reason, verified_running = verify_job_origin(
                origin, status, job_id=request["job_id"], remote_state_dir=expected_state_dir,
                expected_nonce=expected_nonce, expected_launch_cwd=expected_launch_cwd,
                exit_code=exit_code, expected_actual_cwd=expected_actual_cwd)
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            valid, reason, verified_running = False, "origin_record_incomplete", None
            origin = status = {}
            exit_code = None
        if not valid:
            return {"error": "job_identity_unconfirmed", "job_id": request["job_id"],
                    "running": None, "exit_code": None, "execution_state": "unknown",
                    "state_reason": reason}
    else:
        origin = status = None
        verified_running = None
    exit_path = path / "exit_code"
    if expected_nonce is not None:
        running = verified_running
        exit_code = int(exit_path.read_text()) if exit_path.exists() else None
        detail = {}
    elif exit_path.exists():
        running, exit_code = False, int(exit_path.read_text())
        detail = {}
    else:
        try:
            origin = json.loads((path / "origin.json").read_text())
            if not isinstance(origin, dict):
                origin = {}
        except (OSError, ValueError):
            origin = {}
        running, detail = job_liveness(origin)
        exit_code = None
    result = {"job_id": request["job_id"], "running": running, "exit_code": exit_code,
              "execution_state": "running" if running else ("finished" if exit_code is not None else "unknown"), **detail}
    if (path / "status.json").exists():
        result["remote_task"] = json.loads((path / "status.json").read_text())
        result["job_dir"] = str(path)
        if result["remote_task"].get("cleanup_state") == "unconfirmed":
            result["execution_state"] = "unknown"
    for stream in ("stdout", "stderr"):
        cursor = request[stream + "_cursor"]
        logfile = path / (stream + ".log")
        with logfile.open("rb") as source:
            if cursor > logfile.stat().st_size:
                raise ValueError("Log cursor exceeds available bytes")
            source.seek(cursor)
            data = source.read(request["budget"])
            # Don't discard lines or consume an incomplete UTF-8 character.
            # The next page rereads boundary bytes, keeping the strict budget.
            lines = data.splitlines(keepends=True)
            if len(lines) > request["tail_lines"]:
                data = b"".join(lines[:request["tail_lines"]])
            try:
                data.decode("utf-8")
            except UnicodeDecodeError as exc:
                if exc.reason == "unexpected end of data":
                    data = data[:exc.start]
        result[stream + "_tail"] = data.decode("utf-8", "replace")
        result["next_" + stream + "_cursor"] = cursor + len(data)
        result[stream + "_has_more"] = cursor + len(data) < logfile.stat().st_size
    return result


def main():
    request = json.load(sys.stdin)
    try:
        if request["op"] in {"job_start", "job_status"}:
            result = job(request)
        elif request["op"] == "apply_patch":
            result = apply_patch(request)
        elif request["op"] == "read_selected":
            result = read_selected(request)
        elif request["op"] == "read_lines":
            result = read_lines(request)
        elif request["op"] == "read_binary":
            result = read_binary(request)
        else:
            result = edit(request)
    except (OSError, ValueError, UnicodeError, subprocess.SubprocessError) as exc:
        result = {"error": "remote_operation_failed", "message": str(exc)}
    marker = "\x1eREMOTE_WORKBENCH_RESULT_" + request.get("_response_id", "") + ":\x1f"
    print(marker + json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
