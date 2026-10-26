"""Read-only remote log follower, sent over the existing mux; no workload launch."""
import base64


def follow_logs(settings):
    root = Path(settings["job_dir"])
    cursors = dict(settings["cursors"])
    prefix = settings["prefix"]

    def emit(value):
        print(prefix + json.dumps(value, separators=(",", ":")), flush=True)

    last_heartbeat = time.monotonic()
    last_status = None
    terminal_pending_since = None
    while True:
        ready, _, _ = select.select([sys.stdin.buffer], [], [], 0)
        if ready:
            data = os.read(sys.stdin.fileno(), 4096)
            if not data:
                return
            last_heartbeat = time.monotonic()
        if time.monotonic() - last_heartbeat > 15:
            return
        try:
            expected_nonce = settings.get("expected_origin_nonce")
            expected_identity = settings.get("expected_identity")
            has_expected_identity = expected_nonce is not None or expected_identity is not None
            if has_expected_identity:
                if root.is_symlink() or any((root / name).is_symlink()
                                            for name in ("origin.json", "status.json", "exit_code",
                                                         "stdout.log", "stderr.log")):
                    raise ValueError("job_identity_path_is_symlink")
                origin = json.loads((root / "origin.json").read_text())
                status = json.loads((root / "status.json").read_text())
                exit_path = root / "exit_code"
                code = int(exit_path.read_text()) if exit_path.exists() else None
                valid, reason, running = verify_job_origin(
                    origin, status, job_id=settings.get("expected_job_id"),
                    remote_state_dir=settings.get("expected_remote_state_dir"),
                    expected_nonce=expected_nonce,
                    expected_launch_cwd=settings.get("expected_launch_cwd"),
                    exit_code=code, expected_identity=expected_identity,
                    expected_actual_cwd=settings.get("expected_actual_cwd"))
                if not valid:
                    if (reason == "terminal_record_incomplete" and
                            status.get("state") == "finished" and code is None):
                        if terminal_pending_since is None:
                            terminal_pending_since = time.monotonic()
                        if time.monotonic() - terminal_pending_since < 1.0:
                            time.sleep(0.025)
                            continue
                    emit(dict(type="error", message=reason or "job_identity_unconfirmed",
                              state_reason=reason or "job_identity_unconfirmed"))
                    return
                terminal_pending_since = None
            else:
                try:
                    status = json.loads((root / "status.json").read_text())
                except FileNotFoundError:
                    status = {}
                origin = {}
                code = int((root / "exit_code").read_text()) if (root / "exit_code").exists() else None
                running = False if code is not None else None
            if not has_expected_identity and code is None:
                origin = json.loads((root / "origin.json").read_text())
                running, detail = job_liveness(origin)
            elif has_expected_identity:
                detail = {}
                if status.get("cleanup_state") == "unconfirmed":
                    emit(dict(type="status", running=None, exit_code=code,
                              execution_state="unknown", remote_task=status,
                              state_reason="cleanup_unconfirmed", complete=False))
                    return
            else:
                detail = {}
            sizes = {}
            for stream in ("stdout", "stderr"):
                path = root / (stream + ".log")
                sizes[stream] = path.stat().st_size
                if sizes[stream] < cursors[stream]:
                    raise ValueError("remote log was truncated")
                with path.open("rb") as source:
                    source.seek(cursors[stream])
                    data = source.read(24576)
                if data:
                    emit(dict(type="data", stream=stream, offset=cursors[stream],
                              data=base64.b64encode(data).decode("ascii")))
                    cursors[stream] += len(data)
            complete = code is not None and cursors == sizes
            value = dict(type="status", running=running, exit_code=code,
                         execution_state="unknown" if status.get("cleanup_state") == "unconfirmed"
                         else "finished" if code is not None else "running" if running else "unknown",
                         remote_task=status, remote_bytes=sizes, complete=complete, **detail)
            if value != last_status:
                emit(value)
                last_status = value
            if complete or running is None:
                return
            if cursors == sizes:
                time.sleep(0.15)
        except (OSError, ValueError, TypeError) as exc:
            emit(dict(type="error", message=str(exc)))
            return
