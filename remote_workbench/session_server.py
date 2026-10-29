"""Opt-in Codex thread-aware MCP; control and persistent state stay local."""
from __future__ import annotations

import argparse
from dataclasses import asdict
import hashlib
import inspect
import json
from pathlib import Path
import posixpath
import re
import sqlite3
import sys
import time
import uuid

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.fastmcp.tools.base import Tool

from remote_workbench import audit, job_collector
from remote_workbench.audit_web import handler_for_workspaces
from remote_workbench.config import load_config
from remote_workbench.session_context import get_codex_identity, request_metadata, SessionIdentityUnavailable
from remote_workbench.workspace import TOOLS, bind_tool, _local_audit_viewer
from remote_workbench.workspace_router import SessionRouter, default_state_dir
from remote_workbench.workspace_registry import WorkspaceTarget
from remote_workbench.workspace_snapshot import collect as collect_snapshot
from remote_workbench import baseline
from remote_workbench.auth_boundary import authentication_command, refusal
from remote_workbench.call_arguments import validate_call_arguments
from remote_workbench.edit_spec import PreparedEdit
from remote_workbench.command_spec import Command, normalize_arguments
from remote_workbench.tool_interface import INSTRUCTIONS, TOOL_NAMES, present_result

_LOCAL_TOOLS = {"audit_list", "audit_get", "read_output", "search_output"}
_JOB_CONTROL_TOOLS = {"job_status", "stop_job", "job_wait"}
# Read-only checks and controls must remain available while a foreground
# command owns the workspace lease. Remote checks may observe intermediate
# files while a command is running; they never mutate the workspace.
_READ_ONLY_REMOTE_TOOLS = {"workspace_snapshot", "fetch_image",
                           "read_file", "list_dir", "grep", "find_files", "git_status", "git_diff"}
_SESSION_LOCAL_TOOLS = {"workspace_resume", "workspace_note"}
_UNLOCKED_TOOLS = _LOCAL_TOOLS | _JOB_CONTROL_TOOLS | {
    "workspace_info", "workspace_history", "resolve_operation"
} | _READ_ONLY_REMOTE_TOOLS | _SESSION_LOCAL_TOOLS
_BUDGETS = {"exec_command": 2048, "read_file": 4096, "patch_file": 4096,
            "git_diff": 4096, "start_job": 8192}


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


def _public_test_attempt(attempt, *, applicability=None, historical=True):
    if not isinstance(attempt, dict):
        return None
    fields = (
        "attempt_id", "attempt_order", "workspace_id", "target_id", "job_id",
        "execution_kind", "workspace_revision", "target", "cwd", "started_at",
        "state", "process_exit_status", "exit_code", "test_result",
        "tests_run", "reported_skips", "executed", "passed", "summary",
        "before", "after", "output_id", "finished_at", "test_result_reason", "error",
    )
    value = {key: attempt[key] for key in fields if key in attempt}
    command = attempt.get("command")
    if isinstance(command, str):
        value["command_preview"] = command[:240] + ("…" if len(command) > 240 else "")
    effective_historical = historical if applicability is not True else False
    value.update(
        applies_to_current_code=applicability,
        historical=effective_historical,
        test_execution_is_historical=True,
        cache_freshness="unknown",
        current_applicability=("unknown" if applicability is None else
                               "current" if applicability is True else "historical"),
        environment_verified_current=False,
        environment_limit="Activated shell environment, installed dependencies and external inputs were not fingerprinted.",
    )
    return value


def create_identity_probe(*, viewer_url: str | None = None, audit_dir: Path) -> FastMCP:
    """A local diagnostic with no host configuration or remote execution tools."""
    server = FastMCP("remote-workbench-identity-probe", instructions=(
        "Read-only local MCP metadata diagnostic. session_identity_probe reports the conversation "
        "identity supplied by the actual client. It does not connect to SSH or select a workspace. "
        "Include the returned operation_log link in your user-facing result."
    ))

    @server.tool(structured_output=False)
    async def session_identity_probe(ctx: Context) -> str:
        meta = request_metadata(ctx)
        result = {"identity_available": False, "identity_source": "mcp._meta.threadId",
                  "metadata_keys": sorted(k for k in meta if isinstance(k, str))[:32],
                  "remote_execution": False}
        try:
            identity = get_codex_identity(ctx)
            result.update(identity_available=True, thread_id=identity.thread_id,
                          client_session_id=identity.session_id)
        except SessionIdentityUnavailable:
            result["message"] = "The client did not supply a usable threadId; session routing remains disabled"
        with audit.scoped_root(audit_dir):
            record = audit.create(kind="tool", tool="session_identity_probe", status="finished", **result)
            result["operation_id"] = record
            if viewer_url:
                result["operation_log"] = f"[**Remote Workbench 操作记录**]({viewer_url}/?id={record})"
        return json.dumps(result, ensure_ascii=False, separators=(",", ":"))

    return server


class SessionMCP(FastMCP):
    async def call_tool(self, name, arguments):
        try:
            arguments = normalize_arguments(name, arguments)
            selected = self._tool_manager.get_tool(name)
            if selected is not None:
                unknown = set(arguments) - set(selected.parameters.get('properties', {}))
                if unknown:
                    raise ValueError('Unknown argument names: ' + ', '.join(sorted(unknown)))
            return await super().call_tool(name, arguments)
        except ValueError as exc:
            raise ToolError(str(exc)) from exc
        except ToolError as exc:
            from pydantic import ValidationError
            cause = exc
            while cause is not None:
                if isinstance(cause, ValidationError):
                    errors = cause.errors(include_input=False, include_url=False)[:4]
                    message = '; '.join('.'.join(map(str, e['loc'])) + ': ' + e['msg'] for e in errors)
                    raise ToolError('Invalid arguments: ' + message) from None
                cause = cause.__cause__
            raise

    async def list_tools(self):
        # One advertised catalog. Old advanced names use the same handlers as
        # the gateway, so cached clients can finish work while reloading.
        def wire_schema(value):
            # Pydantic's generated display titles repeat parameter/definition
            # names. They do not affect validation or client argument shape.
            if isinstance(value, dict):
                return {k: wire_schema(v) for k, v in value.items()
                        if not (k == 'title' and isinstance(v, str)) and not (k == 'default' and v is None)}
            if isinstance(value, list):
                return [wire_schema(v) for v in value]
            return value
        return [tool.model_copy(update={'inputSchema': wire_schema(tool.inputSchema)})
                for tool in await super().list_tools()
                if tool.name in TOOL_NAMES or tool.name == "workspace_tools"]


def create_server(router: SessionRouter, *, viewer_url: str | None = None,
                  tool_profile: str | None = None) -> FastMCP:
    # Accepted solely for old launchers/importers; it cannot select behavior.
    if tool_profile not in {None, "full", "compact"}:
        raise ValueError("tool_profile has been removed; omit this argument")
    handlers = {}
    schemas = {}
    executors = {}
    linked_workspaces = set()
    server = SessionMCP("remote-workbench-session", instructions=INSTRUCTIONS)

    def workspace_url(state):
        return f"{viewer_url}/w/{state.workspace_id}/" if viewer_url else None

    def pack(result, state, name=None, arguments=None):
        result = present_result(name, dict(result), arguments)
        url = workspace_url(state)
        following = result.get('command_result', {})
        needs_link = (state.workspace_id not in linked_workspaces
                      or name in {"workspace_info", "workspace_switch", "workspace_use_local", "workspace_resume"}
                      or result.get("error") or result.get("initial_error") or result.get("timed_out")
                      or result.get("exit_code") not in (None, 0)
                      or following.get('error') or following.get('timed_out')
                      or following.get('exit_code') not in (None, 0))
        if url and needs_link:
            result["operation_log"] = f"[**Remote Workbench 操作记录**]({url})"
            linked_workspaces.add(state.workspace_id)
        return json.dumps(result, ensure_ascii=False, separators=(",", ":"), default=str)

    async def info(state, identity, **arguments):
        from remote_workbench import __version__
        mode = "remote" if state.host is not None else "local"
        return dict(asdict(state), mode=mode, bound=state.host is not None, control="local",
                    directory_selection_required=router.requires_directory_selection(state),
                    execution="remote-workspace-tools" if state.host is not None else "native-local-tools",
                    directory_mode=router.registry.directory_mode(state.target_id) if state.host else None,
                    project_isolated=False, identity_source="mcp._meta.threadId",
                    client_session_id=identity.session_id, allowed_hosts=sorted(router.config.hosts),
                    runtime={"version": __version__, "package_path": str(Path(__file__).parent),
                             "python_executable": sys.executable, "config_path": router.config.source_path,
                             "data_dir": str(router.state_dir)},
                    remote_state_dir=router.registry.direct_state_dir(state.target_id) if state.host else None,
                    shell_state_reused=False, managed_write_claims=[c for c in router.resources.active() if c["workspace_id"] == state.workspace_id])

    async def history(state, identity, limit: int = 20):
        return {"targets": router.registry.history(state.workspace_id, limit)}

    async def resume(state, identity, job_offset: int = 0, limit: int = 20,
                     include_legacy_test_history: bool = False):
        """Build a local-only recovery summary; legacy test history is opt-in."""
        if isinstance(job_offset, bool) or not isinstance(job_offset, int) or job_offset < 0:
            raise ValueError("job_offset must be a non-negative integer")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 50:
            raise ValueError("limit must be between 1 and 50")
        if not isinstance(include_legacy_test_history, bool):
            raise ValueError("include_legacy_test_history must be a boolean")
        job_page = router.registry.list_jobs(
            state.workspace_id, limit=limit + 1, offset=job_offset)
        jobs_has_more = len(job_page) > limit
        jobs = []
        for item in job_page[:limit]:
            target = item["target"]
            local = None
            lookup_error = None
            if target.host in router.config.hosts and target.cwd:
                try:
                    executor = router.snapshot(state, target).workspace.executor
                    lookup = getattr(executor, "job_local_info", None)
                    value = lookup(item["job_id"]) if lookup is not None else None
                    if isinstance(value, dict):
                        local = value
                except (OSError, ValueError, sqlite3.Error) as exc:
                    lookup_error = type(exc).__name__
            local_summary = None
            if local is not None:
                local_summary = {
                    key: local[key] for key in (
                        "job_output_id", "running", "exit_code", "execution_state",
                        "sync_state", "stdout_bytes", "stderr_bytes", "synced_at", "error",
                    ) if key in local
                }
                if "execution_state" not in local_summary:
                    local_summary["execution_state"] = (
                        "running" if local.get("running") is True else "unknown"
                    )
            jobs.append({
                "job_id": item["job_id"],
                "target": asdict(target),
                "local_status": local_summary,
                "local_cache_age": "unknown",
                "lookup_error": lookup_error,
            })
        legacy_test_history = None
        if include_legacy_test_history:
            current_test = (router.registry.last_test(state.workspace_id, state.target_id)
                            if state.target_id else None)
            latest_attempt = (router.registry.latest_test_attempt(state.workspace_id, state.target_id)
                              if state.target_id else None)
            latest_attempt = _public_test_attempt(latest_attempt, applicability=None, historical=True)
            if isinstance(current_test, dict):
                current_test = dict(current_test)
                # Explicit legacy reads are historical only; never rewrite the stored row.
                current_test.update(
                    applies_to_current_code=None,
                    historical=True,
                    test_execution_is_historical=True,
                    current_code_checked=False,
                    current_applicability="unknown",
                    cache_freshness="unknown",
                    environment_verified_current=False,
                )
                command = current_test.pop("command", None)
                if isinstance(command, str):
                    current_test["command_preview"] = command[:240] + ("…" if len(command) > 240 else "")
                current_test["local_record_id"] = (
                    current_test.get("operation_id") or current_test.get("output_id")
                )
                for field in ("before", "after"):
                    evidence = current_test.get(field)
                    if not isinstance(evidence, dict):
                        continue
                    code = evidence.get("code")
                    current_test[field] = {
                        "code": ({
                            key: code[key] for key in ("complete", "digest", "scope", "files", "bytes", "reason")
                            if key in code
                        } if isinstance(code, dict) else None),
                        "config_digest": evidence.get("config_digest"),
                        "observed_at": evidence.get("observed_at"),
                    }
            legacy_test_history = {
                "last_test": current_test,
                "latest_test_attempt": latest_attempt,
                "applicability": "unknown",
                "source": "local_legacy_history",
            }
        result = {
            "binding": dict(asdict(state), mode="remote" if state.host is not None else "local",
                            directory_selection_required=router.requires_directory_selection(state)),
            "recent_bindings": router.registry.history(state.workspace_id, 10),
            "jobs": jobs,
            "jobs_offset": job_offset,
            "jobs_limit": limit,
            "jobs_has_more": jobs_has_more,
            "jobs_next_offset": job_offset + limit if jobs_has_more else None,
            "jobs_truncated": jobs_has_more,
            "note": router.registry.workspace_note(state.workspace_id),
            "remote_query_performed": False,
            "job_collection_restarted": False,
            "local_cache_freshness": "unknown; no remote status query was made",
        }
        if legacy_test_history is not None:
            result["legacy_test_history"] = legacy_test_history
        return result

    async def note(state, identity, goal: str | None = None, constraints: str | None = None,
                   decisions: str | None = None, next_step: str | None = None):
        values = {"goal": goal, "constraints": constraints,
                  "decisions": decisions, "next_step": next_step}
        updates = {key: value for key, value in values.items() if value is not None}
        if any(len(value) > 2000 for value in updates.values()):
            raise ValueError("Each workspace note field must be at most 2000 characters")
        saved = (router.registry.workspace_note(state.workspace_id, updates) if updates
                 else router.registry.workspace_note(state.workspace_id))
        return {"note": saved, "updated": bool(updates), "storage": "local_workspace_registry"}

    async def resolve_operation(state, identity, idempotency_key: str):
        """Recover a durable job handle after an MCP response was lost."""
        if not isinstance(idempotency_key, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", idempotency_key):
            raise ValueError("idempotency_key must contain 1..128 ASCII letters, digits, '-' or '_'")
        operation = router.registry.get_job_operation(state.workspace_id, idempotency_key)
        local_job = None
        local_job_error = None
        if operation.target.host not in router.config.hosts:
            local_job_error = "origin_host_not_configured"
        elif not operation.target.cwd:
            local_job_error = "origin_target_cwd_unavailable"
        else:
            try:
                executor = router.snapshot(state, operation.target).workspace.executor
                origin = _job_origin_record(
                    operation.target, operation.job_id,
                    origin_nonce=operation.origin_nonce,
                    launch_cwd=operation.launch_cwd,
                    remote_state_dir=operation.remote_state_dir,
                    ssh_target=operation.ssh_target,
                    actual_cwd=operation.actual_cwd,
                )
                # resolve_operation may be called while the thread is bound
                # to another target. Handle/audit creation and collector tasks
                # must inherit the saved job's identity, never the current B
                # binding, while leaving that active binding untouched.
                with audit.scoped_metadata(
                        thread_id=state.thread_id, workspace_id=state.workspace_id,
                        revision=state.revision, target_id=operation.target.target_id,
                        client_session_id=identity.session_id):
                    local_job = await job_collector.ensure(executor, operation.job_id, origin=origin)
                    if (isinstance(local_job, dict) and local_job.get("job_output_id") and
                            not local_job.get("error") and local_job.get("execution_state") != "unknown" and
                            local_job.get("actual_cwd")):
                        router.registry.update_job_actual_cwd(
                            state.workspace_id, operation.job_id, local_job["actual_cwd"])
            except (OSError, ValueError, sqlite3.Error, RuntimeError) as exc:
                local_job_error = "origin_recovery_unavailable: " + str(exc)
        result = {
            "idempotency_key": operation.idempotency_key,
            "operation_state": operation.state,
            "job_id": operation.job_id,
            "target": asdict(operation.target),
            "created_at": operation.created_at,
            "updated_at": operation.updated_at,
            "execution_state": (local_job.get("execution_state", "unknown")
                                 if isinstance(local_job, dict) else "unknown"),
            "next_step": "Use job_status or job_wait to inspect the same job; both route to its saved target.",
        }
        if isinstance(local_job, dict):
            router.resources.reconcile(state.workspace_id, operation.job_id, local_job)
            result["local_job"] = local_job
            if local_job.get("error"):
                result["local_job_state"] = "unavailable"
                result["state_reason"] = (local_job.get("state_reason") or
                                           local_job.get("sync_error") or local_job["error"])
        if local_job_error:
            result["local_job_state"] = "unavailable"
            result["state_reason"] = local_job_error
        if operation.state in {"reserved", "unknown"}:
            result["warning"] = (
                "Submission may have reached the remote worker. Do not restart it under a new key "
                "until its state is checked."
            )
        return result

    async def workspace_snapshot(state, identity, scan_content: bool = False,
                                 max_files: int = 10000,
                                 max_content_bytes: int = 67108864):
        if not isinstance(scan_content, bool):
            raise ValueError("scan_content must be a boolean")
        if (isinstance(max_files, bool) or not isinstance(max_files, int)
                or not 1 <= max_files <= 100000):
            raise ValueError("max_files must be between 1 and 100000")
        if (isinstance(max_content_bytes, bool) or not isinstance(max_content_bytes, int)
                or not 1 <= max_content_bytes <= 1073741824):
            raise ValueError("max_content_bytes must be between 1 and 1073741824")
        snapshot = router.snapshot(state)
        record = audit.create(kind="tool", tool="workspace_snapshot", host=snapshot.host,
                              cwd=snapshot.cwd, status="running")
        token = audit.operation_id.set(record)
        try:
            result = await collect_snapshot(snapshot)
            if scan_content:
                result["content_fingerprint"] = await baseline.project_fingerprint(
                    snapshot.workspace.executor, snapshot.project_root,
                    max_files=max_files, max_bytes=max_content_bytes)
                result["content_scan_budget"] = {
                    "max_files": max_files,
                    "max_content_bytes": max_content_bytes,
                }
            result["communication"] = router.registry.metrics(state.workspace_id)
            result["communication"]["scope"] = "recorded_workspace_responses_before_this_call"
            result["operation_id"] = record
            audit.update(record, status="finished", output_id=result["output_id"])
            return result
        except BaseException as exc:
            audit.update(record, status="failed", error=str(exc))
            raise
        finally:
            audit.operation_id.reset(token)

    def record_metrics(state, name, arguments, text, started, *, result=None, failed=False):
        read_key = None
        if name == "read_output" and result is not None:
            # Repeat detection concerns identical saved-output pages only.
            # New bytes appended to a running stream produce a different key.
            fields = [arguments.get("output_id"), arguments.get("stream"),
                      arguments.get("cursor"), result.get("content")]
            read_key = hashlib.sha256(json.dumps(fields, ensure_ascii=False).encode()).hexdigest()
        try:
            router.registry.record_call(state.workspace_id, name,
                len(json.dumps(arguments, ensure_ascii=False, separators=(",", ":")).encode()),
                len(text.encode()), round((time.monotonic() - started) * 1000), failed, read_key)
        except (sqlite3.Error, OSError):
            # An accounting failure must not turn an already executed write
            # into an apparent tool failure that invites a dangerous retry.
            print("Remote Workbench communication accounting unavailable", file=sys.stderr)

    async def switch(state, identity, host: str, cwd: str, expected_revision: int | None = None,
                     command: Command | None = None, max_bytes: int = 4096,
                     timeout: int | None = None, compute_review: dict | None = None,
                     read_files: list[str] | None = None,
                     edit_files: list[str] | None = None):
        selected = [(p, False) for p in (read_files or [])] + [(p, True) for p in (edit_files or [])]
        if (len(selected) > 16 or len({p for p, _ in selected}) != len(selected)
                or any(not p or '\x00' in p or p.startswith('~') for p, _ in selected)):
            raise ValueError('Select at most 16 distinct file paths without NUL or ~')
        record = audit.create(kind="tool", tool="workspace_switch", status="running",
                              arguments={"host": host, "cwd": cwd, "expected_revision": expected_revision})
        token = audit.operation_id.set(record)
        try:
            updated = await router.switch(state, host, cwd, expected_revision)
            audit.update(record, status="finished", host=updated.host, cwd=updated.cwd,
                         target_id=updated.target_id, revision=updated.revision, previous=asdict(state))
            result = dict(asdict(updated), bound=True, project_isolated=False,
                          directory_mode=router.registry.directory_mode(updated.target_id),
                          previous={"host": state.host, "cwd": state.cwd, "revision": state.revision},
                          operation_id=record, protocol_version=2,
                          remote_state_dir=router.registry.direct_state_dir(updated.target_id))
            if command is not None:
                # The switch already owns the lease. Execute against its committed
                # target directly, without a nested lease or a new model round.
                try:
                    result["initial"] = await executors["exec_command"](
                        updated, identity, command=command, cwd=".", timeout=timeout,
                        max_bytes=max_bytes, compute_review=compute_review)
                except Exception as exc:
                    # Binding succeeded; never misreport it as a failed switch.
                    result["initial_error"] = str(exc)
            if selected:
                initial = result.get('initial', {})
                if (result.get('initial_error') or initial.get('error') or initial.get('timed_out')
                        or initial.get('executed') is False or initial.get('exit_code') not in (None, 0)):
                    result['files_skipped'] = 'initial_command_not_successful'
                else:
                    result['files'] = []
                    for path, for_edit in selected:
                        try:
                            item = await executors['read_file'](updated, identity, path=path,
                                offset=0, limit=160, max_bytes=max_bytes, for_edit=for_edit, local_copy=False)
                        except Exception as exc:
                            item = {'error': 'read_failed', 'message': str(exc)[:512]}
                        result['files'].append(dict(item, requested_path=path))
            return result
        except BaseException as exc:
            audit.update(record, status="failed", error=str(exc))
            raise
        finally:
            audit.operation_id.reset(token)

    async def use_local(state, identity, expected_revision: int | None = None):
        record = audit.create(kind="tool", tool="workspace_use_local", status="running",
                              arguments={"expected_revision": expected_revision})
        token = audit.operation_id.set(record)
        try:
            updated = router.use_local(state, expected_revision)
            audit.update(record, status="finished", revision=updated.revision,
                         previous=asdict(state), mode="local")
            return dict(asdict(updated), mode="local", bound=False,
                        previous={"host": state.host, "cwd": state.cwd,
                                  "revision": state.revision}, operation_id=record)
        except BaseException as exc:
            audit.update(record, status="failed", error=str(exc))
            raise
        finally:
            audit.operation_id.reset(token)

    def register(name, signature, description, execute):
        executors[name] = execute
        # Context is injected by FastMCP and deliberately absent from JSON Schema.
        signature = signature.replace(parameters=[*signature.parameters.values(),
            inspect.Parameter("ctx", inspect.Parameter.KEYWORD_ONLY, annotation=Context, default=None)],
            return_annotation=str)

        async def invoke(**kwargs):
            started = time.monotonic()
            bound = signature.bind(**kwargs)
            bound.apply_defaults()
            arguments = dict(bound.arguments)
            ctx = arguments.pop("ctx")
            try:
                identity = get_codex_identity(ctx)
            except ValueError as exc:
                raise ToolError(str(exc)) from exc
            state = router.registry.get_or_create(identity.thread_id)
            directory = router.audit_dir(state.workspace_id)
            directory.mkdir(parents=True, mode=0o700, exist_ok=True)
            try:
                async def run_scoped(current):
                    metadata = dict(thread_id=identity.thread_id, workspace_id=current.workspace_id,
                                    revision=current.revision, target_id=current.target_id,
                                    client_session_id=identity.session_id)
                    with audit.scoped_root(directory), audit.scoped_metadata(**metadata):
                        result = await execute(current, identity, **arguments)
                        text = pack(result, router.registry.get(current.workspace_id), name, arguments)
                        initial = result.get("initial", {})
                        following = result.get('command_result', {})
                        failed = bool(result.get("error") or result.get("initial_error") or result.get("timed_out") or
                                      result.get("exit_code") not in (None, 0) or
                                      initial.get("error") or initial.get("timed_out") or
                                      initial.get("exit_code") not in (None, 0) or
                                      following.get('error') or following.get('timed_out') or
                                      following.get('exit_code') not in (None, 0))
                        record_metrics(current, name, arguments, text, started, result=result, failed=failed)
                        return text

                if name in _UNLOCKED_TOOLS:
                    # Re-read current workspace state without waiting on a
                    # command's lease. Job tools resolve their immutable origin
                    # below, while local tools stay within this workspace audit.
                    state = router.registry.get(state.workspace_id)
                    return await run_scoped(state)
                async with router.lease(state.workspace_id):
                    state = router.registry.get(state.workspace_id)
                    return await run_scoped(state)
            except Exception as exc:
                url = workspace_url(state)
                link = f"\n\n[**Remote Workbench 操作记录**]({url})" if url else ""
                text = str(exc) + link
                record_metrics(state, name, arguments, text, started, failed=True)
                raise ToolError(text) from exc

        invoke.__name__ = name
        invoke.__doc__ = description
        invoke.__signature__ = signature
        invoke.__annotations__ = {p.name: p.annotation for p in signature.parameters.values()}
        invoke.__annotations__["return"] = str
        handlers[name] = invoke
        schemas[name] = Tool.from_function(invoke, structured_output=False)
        if name == 'read_file':
            async def read_file(path: str, offset: int = 0, limit: int = 160,
                                max_bytes: int = 4096, for_edit: bool = False, ctx: Context = None) -> str:
                return await handlers['read_file'](path=path, offset=offset, limit=limit,
                                                   max_bytes=max_bytes, for_edit=for_edit, local_copy=False, ctx=ctx)
            read_file.__doc__ = 'Read numbered lines; for_edit=true returns an edit_id for guarded updates. No probe or metadata discovery is needed.'
            server.tool(structured_output=False)(read_file)
        elif name == "apply_patch":
            # The primary editing schema has no model-authored patch language.
            # Keep the compatible raw form in schemas[] for the advanced gateway.
            async def structured_apply_patch(edits: list[PreparedEdit], cwd: str = ".", check_only: bool = False,
                                             command: Command | None = None, timeout: int | None = None,
                                             max_bytes: int = 4096, compute_review: dict | None = None,
                                             ctx: Context = None) -> str:
                return await handlers["apply_patch"](edits=edits, cwd=cwd, check_only=check_only,
                    command=command, timeout=timeout, max_bytes=max_bytes, compute_review=compute_review, ctx=ctx)
            structured_apply_patch.__name__ = "apply_patch"
            structured_apply_patch.__doc__ = (
                "Read with for_edit=true, then edits=[{edit_id,new:[source lines]}] for the whole file; optional start_line/end_line select a range. No old text or patch markers. "
                "command optionally runs explicit checks only after successful apply, under the same binding/write claim. Returns next edit_ids. "
                "check_only previews; cannot combine with command. Raw patches/Add/Delete: workspace_tools('apply_patch').")
            server.tool(structured_output=False)(structured_apply_patch)
            tool_metadata = server._tool_manager.get_tool("apply_patch")
            model = tool_metadata.fn_metadata.arg_model
            model.model_config['extra'] = 'forbid'
            model.model_rebuild(force=True)
            tool_metadata.parameters = model.model_json_schema()
        else:
            server.tool(structured_output=False)(invoke)

    for name, function, description in (
        ("workspace_info", info, "Inspect this thread's persistent workspace and metadata identity. No remote execution."),
        ("workspace_history", history, "Read this thread's previous host/source-directory bindings, newest first."),
        ("workspace_resume", resume, "Build a local-only recovery summary of this thread's binding, saved jobs and note; legacy test history is read only when include_legacy_test_history=true."),
        ("workspace_note", note, "Read or update this thread's small local recovery note (goal, constraints, decisions, next_step)."),
        ("resolve_operation", resolve_operation, "Recover the same background job by its idempotency_key after a lost response. This may make a read-only SSH status request to the saved origin, then reconnects local log collection; it never starts a second job."),
        ("workspace_snapshot", workspace_snapshot, "Inspect current remote Git state and measured response bytes. Project contents are not scanned unless scan_content=true; then the scan is bounded by max_files and max_content_bytes. No test history is compared."),
        ("workspace_switch", switch, "Bind an explicit host and actual cwd without copying code. Jobs retain their original target when this chat switches directories."),
        ("workspace_use_local", use_local, "Clear this thread's active remote binding and return to native local tools. Remote targets, private copies, history, and job origins remain available."),
    ):
        signature = inspect.signature(function, eval_str=True)
        signature = signature.replace(parameters=[p for p in signature.parameters.values()
                                                  if p.name not in {"state", "identity", "arguments"}])
        register(name, signature, description, function)

    for name, function, description in TOOLS:
        signature = inspect.signature(function, eval_str=True)
        public = signature.replace(parameters=[
            p.replace(default=".") if p.name == "cwd" else p.replace(default=_BUDGETS[name])
            if p.name == "max_bytes" and name in _BUDGETS else p
            for p in signature.parameters.values() if p.name != "host"])
        if name == 'apply_patch':
            public = public.replace(parameters=[*public.parameters.values(),
                inspect.Parameter('command', inspect.Parameter.POSITIONAL_OR_KEYWORD, default=None, annotation=Command | None),
                inspect.Parameter('timeout', inspect.Parameter.POSITIONAL_OR_KEYWORD, default=None, annotation=int | None),
                inspect.Parameter('max_bytes', inspect.Parameter.POSITIONAL_OR_KEYWORD, default=4096, annotation=int),
                inspect.Parameter('compute_review', inspect.Parameter.POSITIONAL_OR_KEYWORD, default=None, annotation=dict | None)])

        async def execute(state, identity, _name=name, _function=function, **arguments):
            after = None
            if _name == 'apply_patch':
                after_command = arguments.pop('command', None)
                options = {k: arguments.pop(k, default) for k, default in
                           [('timeout', None), ('max_bytes', 4096), ('compute_review', None)]}
                if after_command is not None:
                    if arguments.get('check_only'):
                        raise ValueError('check_only cannot run command')
                    after = dict(options, command=after_command, cwd=arguments.get('cwd', '.'))
                    validate_call_arguments('exec_command', after)
            validate_call_arguments(_name, arguments)
            if _name in _LOCAL_TOOLS:
                return await _function(**arguments)
            target = router.registry.get_job(state.workspace_id, arguments["job_id"]) if _name in _JOB_CONTROL_TOOLS else None
            if target is not None and target.host not in router.config.hosts:
                return {"job_id": arguments["job_id"], "error": "job_origin_unavailable",
                        "running": None, "execution_state": "unknown",
                        "state_reason": "origin_host_not_configured"}
            snapshot = router.snapshot(state, target)
            if after is not None:
                if authentication_command(after['command']):
                    refused = audit.create(kind='tool', tool='apply_patch', status='failed',
                        arguments={'command': '[authentication arguments omitted]'},
                        host=snapshot.host, cwd=snapshot.cwd, error='authentication_requires_user_terminal')
                    return dict(refusal(), operation_id=refused)
                from remote_workbench.compute_review import check_compute_review
                from remote_workbench.call_arguments import command_text
                pending = check_compute_review(command_text(after['command']), snapshot.workspace.resolve_cwd(after['cwd']),
                    snapshot.workspace.executor.config.ssh_target, after['compute_review'],
                    snapshot.workspace.executor.config.compute_review_threshold)
                if pending:
                    return pending
            if _name == 'apply_patch':
                from remote_workbench.edit_spec import prepared_edits
                if prepared_edits(arguments.get('edits')):
                    from remote_workbench.prepared_edit import normalize
                    # Resolve local bytes and origin before acquiring a write claim or contacting SSH.
                    normalize(arguments, snapshot.workspace.executor)
            if (_name in {"exec_command", "start_job"} and
                    authentication_command(arguments.get("command", ""))):
                # Snapshot construction only initializes a local executor. Refuse
                # before job reservation/origin access or remote state lookup.
                # Keep a redacted refusal audit entry.
                record_id = audit.create(
                    kind="tool", tool=_name,
                    arguments={"command": "[authentication arguments omitted]"},
                    host=snapshot.host, cwd=snapshot.cwd, status="failed",
                    error="authentication_requires_user_terminal",
                )
                return dict(refusal(), operation_id=record_id)
            job_origin = None
            if _name in _JOB_CONTROL_TOOLS:
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
            idempotency_key = arguments.pop("idempotency_key", None) if _name == "start_job" else None
            if _name == "start_job":
                origin = WorkspaceTarget(snapshot.target_id, state.workspace_id, snapshot.host,
                                         snapshot.source_cwd, snapshot.cwd, snapshot.project_root)
                launch_cwd = snapshot.workspace.resolve_cwd(arguments.get("cwd"))
                remote_state_dir = snapshot.workspace.executor.remote_state_dir
                ssh_target = snapshot.workspace.executor.config.ssh_target
                candidate_nonce = uuid.uuid4().hex
                if idempotency_key is not None:
                    if (not isinstance(idempotency_key, str) or
                            not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", idempotency_key)):
                        raise ValueError("idempotency_key must contain 1..128 ASCII letters, digits, '-' or '_'")
                    requested_job_id = arguments.get("job_id") or None
                    effective_cwd = launch_cwd
                    timeout = arguments.get("timeout")
                    if timeout is None:
                        timeout = snapshot.workspace.executor.config.job_timeout
                    fingerprint_value = {
                        "command": arguments.get("command"),
                        "cwd": effective_cwd,
                        "timeout": timeout,
                        "requested_job_id": requested_job_id,
                    }
                    fingerprint = hashlib.sha256(json.dumps(
                        fingerprint_value, ensure_ascii=False, sort_keys=True,
                        separators=(",", ":"),
                    ).encode()).hexdigest()
                    job_id = requested_job_id or uuid.uuid4().hex
                    job_operation, created = router.registry.reserve_job_operation(
                        state.workspace_id, idempotency_key, origin, job_id, fingerprint,
                        origin_nonce=candidate_nonce, launch_cwd=launch_cwd,
                        remote_state_dir=remote_state_dir, ssh_target=ssh_target)
                    if not created:
                        if job_operation.state == "rejected":
                            reopened = router.registry.reopen_rejected_job_operation(
                                state.workspace_id, idempotency_key,
                                origin_nonce=candidate_nonce, launch_cwd=launch_cwd,
                                remote_state_dir=remote_state_dir, ssh_target=ssh_target)
                            if reopened:
                                job_operation = router.registry.get_job_operation(
                                    state.workspace_id, idempotency_key)
                            else:
                                job_operation = router.registry.get_job_operation(
                                    state.workspace_id, idempotency_key)
                                return {
                                    "job_id": job_operation.job_id,
                                    "idempotency_key": idempotency_key,
                                    "operation_state": job_operation.state,
                                    "execution_state": "unknown",
                                    "deduplicated": True,
                                    "message": "No new job was submitted. Use resolve_operation to recover this handle.",
                                }
                        else:
                            # A reservation may have reached the server even if
                            # its response was lost. Never start it a second time.
                            return {
                                "job_id": job_operation.job_id,
                                "idempotency_key": idempotency_key,
                                "operation_state": job_operation.state,
                                "execution_state": "unknown",
                                "deduplicated": True,
                                "message": "No new job was submitted. Use resolve_operation to recover this handle.",
                            }
                    arguments["job_id"] = job_operation.job_id
                    job_origin = _job_origin_record(
                        job_operation.target, job_operation.job_id,
                        origin_nonce=job_operation.origin_nonce,
                        launch_cwd=job_operation.launch_cwd,
                        remote_state_dir=job_operation.remote_state_dir,
                        ssh_target=job_operation.ssh_target,
                        actual_cwd=job_operation.actual_cwd,
                    )
                else:
                    job_id = arguments.get("job_id") or uuid.uuid4().hex
                    if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", job_id):
                        raise ValueError("Invalid job_id")
                    arguments["job_id"] = job_id
                    # Even without a request key, reserve the immutable origin
                    # before submission so later job controls cannot drift.
                    router.registry.register_job(
                        state.workspace_id, job_id, origin,
                        origin_nonce=candidate_nonce, launch_cwd=launch_cwd,
                        remote_state_dir=remote_state_dir, ssh_target=ssh_target)
                    job_origin = _job_origin_record(
                        origin, job_id, origin_nonce=candidate_nonce,
                        launch_cwd=launch_cwd, remote_state_dir=remote_state_dir,
                        ssh_target=ssh_target)
            tool = bind_tool(snapshot.workspace, _name, _function, "")
            claim_operation = None
            with audit.scoped_metadata(**snapshot.metadata(), client_session_id=identity.session_id):
                try:
                    writes = _name in {"exec_command", "start_job", "write_file", "patch_file", "apply_patch"}
                    claim = None
                    if writes and not arguments.get("check_only", False):
                        declared_cwd = snapshot.workspace.resolve_cwd(arguments.get("cwd"))
                        resource_root = posixpath.commonpath([snapshot.cwd, declared_cwd])
                        if _name == 'apply_patch' and arguments.get('expected_files'):
                            resource_root = posixpath.commonpath([resource_root, *[
                                posixpath.dirname(path) for path in arguments['expected_files']]])
                        if _name in {"write_file", "patch_file"} and isinstance(arguments.get("path"), str):
                            file_path = posixpath.normpath(posixpath.join(declared_cwd, arguments["path"]))
                            resource_root = posixpath.commonpath([resource_root, posixpath.dirname(file_path)])
                        try:
                            claim = router.resources.acquire(
                                snapshot.workspace.executor.config.ssh_target, resource_root,
                                state.workspace_id, _name, tracked=True)
                        except ValueError:
                            # No remote submission occurred. A caller may retry
                            # the same key when the conflicting workload ends.
                            if job_operation is not None:
                                router.registry.update_job_operation(
                                    state.workspace_id, idempotency_key, "rejected")
                                job_operation = None
                            raise
                    with router.resources.operation(claim) as claim_operation, job_collector.scoped_origin(job_origin):
                        if claim and _name == 'start_job':
                            router.resources.attach_job(claim, arguments['job_id'])
                        result = await tool(**arguments)
                        if _name == 'apply_patch' and result.get('success') and not arguments.get('check_only'):
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
                        if claim:
                            try:
                                router.resources.finish(claim, result.get('command_result', result),
                                    background=_name == "start_job", shell=_name == "exec_command" or 'command_result' in result)
                            except (OSError, sqlite3.Error):
                                # The remote operation already returned; its marker
                                # can reconcile locally once the state store recovers.
                                result['resource_warning'] = 'completion_record_pending'
                except BaseException:
                    if job_operation is not None:
                        try:
                            router.registry.update_job_operation(
                                state.workspace_id, idempotency_key,
                                "unknown" if claim_operation is not None and claim_operation.submitted else "rejected")
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
            if _name in _JOB_CONTROL_TOOLS:
                router.resources.reconcile(state.workspace_id, arguments["job_id"], result)
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
            return result

        register(name, public, description, execute)
    @server.tool(structured_output=False)
    async def workspace_tools(tool: str, arguments: dict | None = None, ctx: Context = None) -> str:
        """Advanced tools by name: jobs, recovery, notes, search/edits. tool='describe', arguments={name:'start_job'} returns just that schema; omit name for available names. Normal tasks use exec_command/apply_patch directly."""
        identity = get_codex_identity(ctx)
        if arguments is not None and not isinstance(arguments, dict):
            raise ToolError("arguments must be an object")
        arguments = arguments or {}
        arguments = normalize_arguments(tool, arguments)
        if tool == "describe":
            name = arguments.get("name")
            if name is None:
                result = {"tools": sorted(handlers)}
            elif name in schemas:
                selected = schemas[name]
                result = {"name": name, "description": selected.description, "inputSchema": selected.parameters}
            else:
                raise ToolError("Unknown tool name")
            state = router.registry.get_or_create(identity.thread_id)
            return pack(result, state)
        if tool not in handlers or "ctx" in arguments:
            raise ToolError("Unknown tool or invalid arguments")
        unknown = set(arguments) - set(schemas[tool].parameters.get("properties", {}))
        if unknown:
            raise ToolError("Unknown argument names: " + ", ".join(sorted(unknown)))
        return await schemas[tool].run(arguments, context=ctx)
    return server


def main():
    parser = argparse.ArgumentParser(description="Local, persistent Codex thread-aware remote workspace MCP (prototype)")
    parser.add_argument("--config", help="Host allowlist YAML; uses the normal Remote Workbench configuration by default")
    parser.add_argument("--data-dir", type=Path, default=default_state_dir(), help="Private local registry/audit directory")
    parser.add_argument("--viewer-port", type=int, default=0, help="Local viewer port (default 0: OS-assigned); falls back if occupied")
    parser.add_argument("--identity-probe", action="store_true", help="Expose only a local metadata diagnostic, without host configuration or remote tools")
    parser.add_argument("--tool-profile", choices=("full", "compact"), default=None,
                        help=argparse.SUPPRESS)  # legacy spelling, no behavior switch
    args = parser.parse_args()
    if args.identity_probe:
        directory = args.data_dir.expanduser().resolve() / "identity-probe"
        with _local_audit_viewer(args.viewer_port, audit_dir=directory) as viewer:
            url = viewer.viewer_url
            create_identity_probe(viewer_url=url, audit_dir=directory).run()
        return
    router = SessionRouter(load_config(args.config), args.data_dir)
    with _local_audit_viewer(args.viewer_port, handler=handler_for_workspaces(router.state_dir / "audit")) as viewer:
        url = viewer.viewer_url
        print(f"Local session audit viewer: {url}", file=sys.stderr, flush=True)
        create_server(router, viewer_url=url, tool_profile=args.tool_profile).run()


if __name__ == "__main__":
    main()
