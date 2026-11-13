"""Session operation execution below the MCP schema and identity adapter."""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import sys
import uuid

from remote_workbench import audit, job_collector
from remote_workbench.call_arguments import (
    COMMAND_PREVIEW_BYTES, selected_files, validate_call_arguments, validate_output_budget,
)
from remote_workbench.tool_runtime import authentication_refusal
from remote_workbench.workspace import bind_tool
from remote_workbench.workspace_registry import WorkspaceTarget

_LOCAL_TOOLS = {"audit_list", "audit_get", "read_output", "search_output"}
_JOB_CONTROL_TOOLS = {"job_status", "stop_job", "job_wait"}


def _command_finished_for_reads(result):
    """A confirmed nonzero exit still supplies useful files for fixing a failure."""
    return (isinstance(result, dict) and result.get('execution_state') == 'finished'
            and type(result.get('exit_code')) is int and not result.get('error')
            and not result.get('timed_out') and result.get('executed') is not False)


def _job_origin_record(target, job_id, *, origin_nonce, launch_cwd, remote_state_dir,
                      ssh_target, actual_cwd=None):
    return {
        "workspace_id": target.workspace_id,
        "job_id": job_id,
        "target_id": target.target_id,
        "host": target.host,
        "source_cwd": target.source_cwd,
        "target_cwd": target.cwd,
        "project_root": target.project_root,
        "origin_nonce": origin_nonce,
        "launch_cwd": launch_cwd,
        "remote_state_dir": remote_state_dir,
        "ssh_target": ssh_target,
        "actual_cwd": actual_cwd,
    }


def _reserve_job(router, state, snapshot, arguments, idempotency_key):
    """Save an immutable launch origin and deduplicate retries before submission."""
    origin = WorkspaceTarget(snapshot.target_id, state.workspace_id, snapshot.host,
                             snapshot.source_cwd, snapshot.cwd, snapshot.project_root)
    launch_cwd = snapshot.workspace.resolve_cwd(arguments.get("cwd"))
    remote_state_dir = snapshot.workspace.executor.remote_state_dir
    ssh_target = snapshot.workspace.executor.config.ssh_target
    candidate_nonce = uuid.uuid4().hex
    if idempotency_key is None:
        job_id = arguments.get("job_id") or uuid.uuid4().hex
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", job_id):
            raise ValueError("Invalid job_id")
        arguments["job_id"] = job_id
        router.registry.register_job(
            state.workspace_id, job_id, origin,
            origin_nonce=candidate_nonce, launch_cwd=launch_cwd,
            remote_state_dir=remote_state_dir, ssh_target=ssh_target)
        return None, _job_origin_record(
            origin, job_id, origin_nonce=candidate_nonce, launch_cwd=launch_cwd,
            remote_state_dir=remote_state_dir, ssh_target=ssh_target), None

    if not isinstance(idempotency_key, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", idempotency_key):
        raise ValueError("idempotency_key must contain 1..128 ASCII letters, digits, '-' or '_'")
    requested_job_id = arguments.get("job_id") or None
    timeout = arguments.get("timeout")
    if timeout is None:
        timeout = snapshot.workspace.executor.config.job_timeout
    fingerprint = hashlib.sha256(json.dumps({
        "command": arguments.get("command"), "cwd": launch_cwd,
        "timeout": timeout, "requested_job_id": requested_job_id,
    }, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    operation, created = router.registry.reserve_job_operation(
        state.workspace_id, idempotency_key, origin, requested_job_id or uuid.uuid4().hex, fingerprint,
        origin_nonce=candidate_nonce, launch_cwd=launch_cwd,
        remote_state_dir=remote_state_dir, ssh_target=ssh_target)
    if not created:
        reopened = False
        if operation.state == "rejected":
            reopened = router.registry.reopen_rejected_job_operation(
                state.workspace_id, idempotency_key,
                origin_nonce=candidate_nonce, launch_cwd=launch_cwd,
                remote_state_dir=remote_state_dir, ssh_target=ssh_target)
            operation = router.registry.get_job_operation(state.workspace_id, idempotency_key)
        if not reopened:
            # The previous reservation may already have reached the server.
            return operation, None, {
                "job_id": operation.job_id, "idempotency_key": idempotency_key,
                "operation_state": operation.state, "execution_state": "unknown",
                "deduplicated": True,
                "message": "No new job was submitted. Use resolve_operation to recover this handle.",
            }
    arguments["job_id"] = operation.job_id
    return operation, _job_origin_record(
        operation.target, operation.job_id, origin_nonce=operation.origin_nonce,
        launch_cwd=operation.launch_cwd, remote_state_dir=operation.remote_state_dir,
        ssh_target=operation.ssh_target, actual_cwd=operation.actual_cwd), None


async def _complete_patch(snapshot, arguments, result, after):
    if (result.get('success') and not arguments.get('check_only')
            and not result.get('error') and not result.get('timed_out')
            and result.get('execution_state') in (None, 'finished')):
        if (result.get('file_verification') or {}).get('failed'):
            if after is not None:
                result['command_skipped'] = 'file_verification_failed'
            return
        if arguments.get('prepared_contents') is not None:
            from remote_workbench.prepared_edit import applied_refs
            try:
                result['edits'] = applied_refs(snapshot.workspace.executor, arguments, result['output_id'])
            except Exception:
                # The write already succeeded. Never turn a local
                # snapshot-cache failure into a retryable write error.
                result['edit_refs_unavailable'] = True
        if after is not None:
            from remote_workbench.tools.commands import ssh_run
            from remote_workbench.call_arguments import command_text
            command_tool = bind_tool(snapshot.workspace, 'exec_command', ssh_run, '')
            audit.update(result['output_id'], following_command=command_text(after['command']),
                         following_execution_state='unknown')
            parent = audit.operation_id.set(result['output_id'])
            try:
                result['command_result'] = await command_tool(**after)
            except Exception as exc:
                result['command_result'] = {'error': 'command_unconfirmed',
                    'execution_state': 'unknown', 'message': str(exc)[:512]}
            finally:
                audit.operation_id.reset(parent)
            try:
                following_id = result['command_result'].get('operation_id')
                if following_id:
                    # ssh_run has its own audited tool record;
                    # its transport command is one level below.
                    audit.update(following_id, parent_id=result['output_id'])
                    audit.add_child(result['output_id'], following_id)
                audit.update(result['output_id'],
                    following_output_id=result['command_result'].get('output_id'),
                    following_exit_code=result['command_result'].get('exit_code'),
                    following_execution_state=result['command_result'].get('execution_state', 'unknown'))
            except OSError:
                # Neither phase may be blindly replayed just
                # because their local cross-link could not save.
                result['audit_warning'] = 'following_command_link_incomplete'
    elif after is not None:
        result['command_skipped'] = 'apply_not_successful'


def _command_refusal(tool, options, cwd, host, config):
    from remote_workbench.call_arguments import command_text
    from remote_workbench.compute_review import check_compute_review

    command = options['command']
    refused = authentication_refusal(tool, command, host=host, cwd=cwd)
    if refused is not None:
        return refused
    commands = command['commands'] if isinstance(command, dict) else [command]
    for item in commands:
        pending = check_compute_review(command_text(item), cwd, config.ssh_target,
            options.get('compute_review'), config.compute_review_threshold)
        if pending:
            return pending
    return None


def make_executor(router, name, function, read_selection):
    async def execute(state, identity, **arguments):
        after = None
        selected = (selected_files(arguments.pop('read_files', None), arguments.pop('edit_files', None))
                    if name in {'exec_command', 'apply_patch'} else [])
        selection_budget = arguments.get('max_bytes', COMMAND_PREVIEW_BYTES)
        if selected:
            validate_output_budget(selection_budget)
        if name == 'apply_patch':
            after_command = arguments.pop('command', None)
            options = {k: arguments.pop(k, default) for k, default in
                       [('timeout', None), ('max_bytes', COMMAND_PREVIEW_BYTES), ('compute_review', None)]}
            if after_command is not None:
                if arguments.get('check_only'):
                    raise ValueError('check_only cannot run command')
                after = dict(options, command=after_command, cwd=arguments.get('cwd', '.'))
                validate_call_arguments('exec_command', after)
        validate_call_arguments(name, arguments)
        if name in _LOCAL_TOOLS:
            return await function(**arguments)
        requested_host = arguments.pop('host', None) if name in {'exec_command', 'apply_patch'} else None
        if requested_host is not None:
            # An explicit target may be selected in the same operation.
            # New chats without one still fail closed in router.snapshot.
            selected_cwd = arguments.get('cwd')
            if not requested_host or not isinstance(selected_cwd, str) or not selected_cwd.startswith('/'):
                raise ValueError('An inline host requires an explicit absolute cwd')
            if name == 'apply_patch' and arguments.get('edits') is not None:
                raise ValueError('Use the existing binding for guarded edits')
            config = router.config.get_host(requested_host)
            reviewed = arguments if name == 'exec_command' else after
            if reviewed is not None:
                refused = _command_refusal(name, reviewed, selected_cwd, requested_host, config)
                if refused is not None:
                    return refused
            # Repeating an already-canonical target has the same semantics
            # as omitting host. Keep the immutable target and revision;
            # a different path/alias or explicit workspace_switch still
            # performs directory validation. Command connection checks,
            # supervision and write coordination remain in the normal path.
            if (requested_host != state.host or selected_cwd != state.cwd
                    or router.requires_directory_selection(state)):
                state = await router.switch(state, requested_host, selected_cwd)
            arguments['cwd'] = '.'
            if after is not None:
                after['cwd'] = '.'
        target = router.registry.get_job(state.workspace_id, arguments["job_id"]) if name in _JOB_CONTROL_TOOLS else None
        if target is not None and target.host not in router.config.hosts:
            return {"job_id": arguments["job_id"], "error": "job_origin_unavailable",
                    "running": None, "execution_state": "unknown",
                    "state_reason": "origin_host_not_configured"}
        snapshot = router.snapshot(state, target)
        if after is not None:
            refused = _command_refusal('apply_patch', after, snapshot.workspace.resolve_cwd(after['cwd']),
                                       snapshot.host, snapshot.workspace.executor.config)
            if refused is not None:
                return refused
        if name == 'apply_patch':
            from remote_workbench.edit_spec import prepared_edits
            if prepared_edits(arguments.get('edits')):
                from remote_workbench.prepared_edit import normalize
                # Resolve local bytes and origin before contacting SSH.
                normalize(arguments, snapshot.workspace.executor)
        if name in {"exec_command", "start_job"}:
            # Snapshot construction only initializes a local executor. Refuse
            # before job reservation/origin access or remote state lookup.
            # Keep a redacted refusal audit entry.
            refused = authentication_refusal(name, arguments.get("command", ""),
                                             host=snapshot.host, cwd=snapshot.cwd)
            if refused is not None:
                return refused
        job_origin = None
        if name in _JOB_CONTROL_TOOLS:
            saved_origin = router.registry.get_job_origin(state.workspace_id, arguments["job_id"])
            job_origin = _job_origin_record(
                saved_origin["target"], arguments["job_id"],
                origin_nonce=saved_origin["origin_nonce"],
                launch_cwd=saved_origin["launch_cwd"],
                remote_state_dir=saved_origin["remote_state_dir"],
                ssh_target=saved_origin["ssh_target"],
                actual_cwd=saved_origin["actual_cwd"],
            )
        job_operation = None
        idempotency_key = arguments.pop("idempotency_key", None) if name == "start_job" else None
        if name == "start_job":
            job_operation, job_origin, deduplicated = _reserve_job(
                router, state, snapshot, arguments, idempotency_key)
            if deduplicated is not None:
                return deduplicated
        tool = bind_tool(snapshot.workspace, name, function, "")
        submission_state = None
        with audit.scoped_metadata(**snapshot.metadata(), client_session_id=identity.session_id):
            try:
                from remote_workbench.resource_claims import submission
                with submission() as submission_state, job_collector.scoped_origin(job_origin):
                    result = await tool(**arguments)
                    if requested_host is not None:
                        result['host'] = snapshot.host
                        result['cwd'] = (result.get('remote_task') or {}).get('actual_cwd') or snapshot.cwd
                    if name == 'apply_patch':
                        await _complete_patch(snapshot, arguments, result, after)
            except BaseException:
                if job_operation is not None:
                    try:
                        router.registry.update_job_operation(
                            state.workspace_id, idempotency_key,
                            "unknown" if submission_state is not None and submission_state.submitted else "rejected")
                    except (sqlite3.Error, OSError, ValueError):
                        print("Remote Workbench job retry state unavailable", file=sys.stderr)
                raise
        if (job_origin is not None and isinstance(result, dict) and
                result.get("job_output_id") and not result.get("error") and
                result.get("execution_state") != "unknown"):
            actual_cwd = result.get("actual_cwd")
            remote_task = result.get("remote_task")
            if not actual_cwd and isinstance(remote_task, dict):
                actual_cwd = remote_task.get("actual_cwd")
            if actual_cwd:
                try:
                    router.registry.update_job_actual_cwd(
                        job_origin["workspace_id"], job_origin["job_id"], actual_cwd)
                except (ValueError, sqlite3.Error, OSError) as exc:
                    result = dict(result, execution_state="unknown",
                                  state_reason="actual_cwd_conflict", error=str(exc))
        if job_operation is not None:
            if result.get("executed") is False:
                operation_state = "rejected"
            elif not result.get("error") and result.get("job_id") == job_operation.job_id:
                operation_state = "submitted"
            else:
                operation_state = "unknown"
            try:
                router.registry.update_job_operation(
                    state.workspace_id, idempotency_key, operation_state)
            except (sqlite3.Error, OSError, ValueError):
                # Do not turn a possibly successful remote start into a
                # tool error that encourages an unkeyed duplicate retry.
                print("Remote Workbench job retry state unavailable", file=sys.stderr)
            result = dict(result, idempotency_key=idempotency_key,
                          operation_state=operation_state, job_id=job_operation.job_id)
        if selected:
            observed = result.get('command_result', result) if name == 'apply_patch' else result
            if name == 'apply_patch' and (not result.get('success') or result.get('error')
                    or result.get('timed_out') or result.get('executed') is False
                    or result.get('execution_state') not in (None, 'finished')):
                result['files_skipped'] = 'apply_not_successful'
            elif (name == 'exec_command' or after is not None) and not _command_finished_for_reads(observed):
                result['files_skipped'] = 'command_not_successful'
            else:
                effective_cwd = ((observed.get('remote_task') or {}).get('actual_cwd')
                                 or snapshot.workspace.resolve_cwd(arguments.get('cwd')))
                result['files'] = await read_selection(snapshot, identity, selected,
                    selection_budget, cwd=effective_cwd,
                    parent_id=result.get('operation_id') or result.get('output_id'))
        return result


    return execute
