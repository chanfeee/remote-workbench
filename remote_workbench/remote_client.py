"""Transport JSON requests over stdin; source and content never use shell interpolation."""
import json
import uuid
from pathlib import Path
from remote_workbench import audit
from remote_workbench.remote_shell import python_command

_SUPERVISOR_SOURCE = (Path(__file__).with_name("remote_shell.py").read_text() + "\n" +
                      Path(__file__).with_name("remote_task.py").read_text())
SOURCE = ("TASK_SUPERVISOR_SOURCE = " + repr(_SUPERVISOR_SOURCE) + "\nexec(TASK_SUPERVISOR_SOURCE)\n" +
          "exec(" + repr(Path(__file__).with_name("native_patch.py").read_text()) + ")\n" +
          Path(__file__).with_name("remote_worker.py").read_text())


async def request(executor, payload, cwd=None, timeout=None):
    payload.setdefault("max_file_size", executor.config.max_file_size)
    payload["_response_id"] = uuid.uuid4().hex
    payload["_task_policy"] = executor.config.task_policy()
    return await executor.run(python_command(SOURCE), cwd=cwd,
        timeout=timeout or executor.config.file_timeout,
        input_data=json.dumps(payload, ensure_ascii=False).encode())


async def edit(executor, payload, cwd=None, max_bytes=8192, timeout=None):
    if not 256 <= max_bytes <= 65536:
        raise ValueError("max_bytes must be between 256 and 65536")
    origin = None
    if payload.get("op") == "job_start":
        from remote_workbench.job_collector import current_origin
        origin = current_origin(executor, payload.get("job_id"))
        if origin is not None:
            from remote_workbench.job_collector import validate_origin
            reason = validate_origin(executor, payload.get("job_id"), origin)
            if reason:
                return {"error": "job_identity_unconfirmed", "job_id": payload.get("job_id"),
                        "execution_state": "unknown", "state_reason": reason}
            nonce = origin.get("origin_nonce")
            if not isinstance(nonce, str):
                return {"error": "job_identity_unconfirmed", "job_id": payload.get("job_id"),
                        "execution_state": "unknown", "state_reason": "origin_nonce_unavailable"}
            payload["origin_nonce"] = nonce
            payload["launch_cwd"] = origin["launch_cwd"]
    result = await request(executor, payload, cwd, timeout=timeout)
    if result.exit_code != 0:
        return {"error": "remote_failed", "message": result.stderr, "output_id": result.output_id,
                "execution_state": result.execution_state}
    # Structured worker output is read from the complete spool, not its preview.
    path = audit.directory(result.output_id) / "stdout"
    maximum = executor.config.max_file_size * 8 + 1048576
    if path.stat().st_size > maximum:
        return {"error": "worker_output_too_large", "output_id": result.output_id}
    try:
        data = path.read_bytes()
        marker = ("\x1eREMOTE_WORKBENCH_RESULT_" + payload["_response_id"] + ":\x1f").encode()
        position = data.index(marker) + len(marker)
        value, _ = json.JSONDecoder().raw_decode(data[position:].decode("utf-8"))
    except ValueError:
        return {"error": "invalid_worker_output", "message": result.stdout, "output_id": result.output_id}
    audit.update(result.output_id, worker_op=payload.get("op"))
    if payload.get("op") == "apply_patch":
        # The human viewer can display the submitted patch directly from disk;
        # don't echo an already supplied patch back through the model channel.
        display_patch = value.pop("normalized_patch", payload["patch"])
        with audit.open_stream(result.output_id, "diff") as output:
            output.write(display_patch.encode("utf-8"))
        audit.update(result.output_id, diff_kind="submitted_patch",
                     check_only=bool(payload.get("check_only")),
                     patch_result="failed" if value.get("error") else "checked" if payload.get("check_only") else "applied")
    full_diff = value.pop("diff", "")
    if full_diff:
        data = full_diff.encode()
        with audit.open_stream(result.output_id, "diff") as output:
            output.write(data)
        audit.update(result.output_id, diff_kind="edit_preview" if payload.get("check_only") else "file_edit")
        value["diff"], value["diff_truncated"] = audit.preview(data, max_bytes)
    value["output_id"] = result.output_id
    if payload.get("op") == "job_start" and value.get("job_dir") and not value.get("error"):
        local = await executor.track_job(value, payload["command"], cwd or executor.config.default_cwd,
                                         origin=origin)
        if isinstance(value.get("remote_task"), dict):
            value["remote_task"] = {key: item for key, item in value["remote_task"].items()
                                    if key != "origin_nonce"}
        if local.get("error"):
            return {**value, **local, "launch_output_id": result.output_id}
        value.update(local)
        value["launch_output_id"] = result.output_id
        value["output_id"] = value["job_output_id"]
    return value
