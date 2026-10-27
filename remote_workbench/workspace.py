"""A local MCP with one immutable remote default workspace; no remote agent."""
from __future__ import annotations

import argparse
import errno
from dataclasses import dataclass, replace
from contextlib import contextmanager, nullcontext
import functools
from http.server import ThreadingHTTPServer
import inspect
import json
from pathlib import Path
import posixpath
import re
import sys
import uuid
from threading import Thread
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError

from remote_workbench import audit
from remote_workbench.config import load_config
from remote_workbench.audit_web import Handler as AuditViewerHandler, handler_for_directory, handler_for_instance
from remote_workbench.server import workspace_executor
from remote_workbench.ssh import SSHExecutor
from remote_workbench.compute_review import REVIEW_INSTRUCTIONS
from remote_workbench.tool_interface import CALL_SOURCE_INSTRUCTIONS
from remote_workbench.call_arguments import validate_call_arguments
from remote_workbench.tools import commands, files, git, search, patch, audit as audit_tools


@dataclass(frozen=True)
class Workspace:
    name: str
    host: str
    cwd: str
    executor: SSHExecutor
    directory_mode: str = "direct"

    def __post_init__(self):
        if self.directory_mode != 'direct':
            raise ValueError('Only actual directories are supported')
        from remote_workbench.remote_directory import reject_managed_copy
        reject_managed_copy(self.cwd)

    @classmethod
    def load(cls, path: str | Path) -> "Workspace":
        path = Path(path).resolve()
        data = json.loads(path.read_text())
        allowed = {"version", "name", "host", "cwd", "config"}
        if not isinstance(data, dict) or set(data) - allowed:
            raise ValueError("Unknown workspace manifest fields")
        if data.get("version") != 1:
            raise ValueError("Workspace version must be 1")
        name, host, cwd = (data.get(key) for key in ("name", "host", "cwd"))
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name):
            raise ValueError("Workspace name must contain 1..64 ASCII letters, digits, '-' or '_'")
        if not isinstance(host, str) or not host:
            raise ValueError("Workspace host is required")
        if not isinstance(cwd, str) or not cwd.startswith("/") or "\x00" in cwd:
            raise ValueError("Workspace cwd must be a remote absolute path")
        config_path = data.get("config")
        if config_path is not None:
            if not isinstance(config_path, str):
                raise ValueError("config must be a path string")
            config_path = path.parent / Path(config_path).expanduser()
        base = load_config(config_path).get_host(host)
        cwd = posixpath.normpath(cwd)
        # Default shells load their own configuration. Do not carry a configured
        # host preamble/activated environment into a workspace automatically.
        config = replace(base, default_cwd=cwd, preamble="")
        return cls(name, host, cwd, SSHExecutor(config))

    def resolve_cwd(self, cwd: str | None = None) -> str:
        if cwd is None or cwd == "":
            return self.cwd
        if "\x00" in cwd or cwd.startswith("~"):
            raise ValueError("cwd must be absolute or relative to the workspace; use an absolute home path")
        resolved = posixpath.normpath(posixpath.join(self.cwd, cwd))
        if self.directory_mode == 'direct':
            from remote_workbench.remote_directory import reject_managed_copy
            reject_managed_copy(resolved)
        return resolved


# Short names and descriptions avoid repeating SSH setup and host parameters in
# every model-facing schema. Original tool validation/audit code remains shared.
TOOLS = [
    ("exec_command", commands.ssh_run, "Execute shell text or an argv array in the remote workspace. Prefer arrays for literal arguments (no shell interpolation); strings support pipes/activation. Fresh account login shell each call; max_bytes bounds output."),
    ("read_file", files.ssh_read_file, "Read numbered UTF-8 lines. for_edit=true returns edit_id/local_path from one complete file snapshot for guarded edits. offset is zero-based; limit/max_bytes bound the preview."),
    ("fetch_image", files.ssh_fetch_image, "Fetch a remote PNG/JPG/PDF/SVG to a private local cache. PDF page is 1-based. Show returned image_markdown in chat; paths and metadata only, no base64. PDF/SVG previews require the local images extra."),
    ("write_file", files.ssh_write_file, "Atomically write remote UTF-8 content. Optional expected_sha256 rejects stale edits."),
    ("patch_file", files.ssh_patch_file, "Apply unique old/new replacements, or sequential edits=[{old,new}], remotely. Optional expected_sha256 and check_only; returns this edit's diff."),
    ("apply_patch", patch.ssh_apply_patch, "Apply patch text or an array of lines: *** Begin Patch context format or unified Git diff. Prefer line arrays; no join/escaping wrapper needed. Update existing files in place. Patch text paths use the Git root if present, otherwise cwd; snapshot edits need no Git. check_only validates without writing."),
    ("list_dir", files.ssh_list_dir, "List remote entries. Relative paths start at the workspace root."),
    ("grep", search.ssh_grep, "Search remote file contents by regex, with bounded results."),
    ("find_files", search.ssh_find_files, "Find remote paths by filename glob, type and depth, with bounded results."),
    ("git_status", git.ssh_git_status, "Read remote repository status, including untracked files."),
    ("git_diff", git.ssh_git_diff, "Default compact numstat; mode='patch' returns paged hunks. Continue with output_id/next_cursor to reuse the snapshot."),
    ("start_job", commands.ssh_run_background, "Start a supervised detached job. Optional wait_seconds waits locally for output/exit, then returns the same job_id; wait expiry never cancels the job. Use an idempotency_key in session mode for safe retries."),
    ("job_status", commands.ssh_job_status, "Read detached job state and incremental output. Reuse returned byte cursors."),
    ("job_wait", commands.ssh_job_wait, "Wait on collector progress or exit without repeated remote-status polling. May start a read-only collector recovery channel; wait expiry does not cancel the job."),
    ("stop_job", commands.ssh_stop_job, "Cancel a supervised job on its originating node. Inspect cleanup_state; remains available when execution is disabled."),
    ("read_output", audit_tools.ssh_read_output, "Page saved output by output_id, stream and byte cursor; full command output and edit diffs are retained locally."),
    ("search_output", audit_tools.ssh_search_output, "Search saved local output with bounded context and byte cursors; no SSH command."),
    ("audit_get", audit_tools.ssh_audit_get, "Read a local operation record, including actual remote commands and child IDs."),
    ("audit_list", audit_tools.ssh_audit_list, "List recent local operation metadata."),
]


def bind_tool(workspace: Workspace, name: str, function, description: str, *, workspace_provider=None, session=None):
    signature = inspect.signature(function, eval_str=True)
    parameters = [p.replace(default=".") if p.name == "cwd" else p
                  for p in signature.parameters.values() if p.name != "host"]
    # All cwd parameters occur after required non-cwd parameters in these tools.
    public_signature = signature.replace(parameters=parameters, return_annotation=dict[str, Any])

    @functools.wraps(function)
    async def invoke(**kwargs):
        # Bind explicitly even for direct Python callers: do not allow **kwargs
        # to smuggle a different host past the public schema.
        bound = public_signature.bind(**kwargs)
        arguments = dict(bound.arguments)
        validate_call_arguments(name, arguments)
        active_workspace = workspace_provider() if workspace_provider else workspace
        if active_workspace.directory_mode == 'direct' and isinstance(arguments.get('path'), str):
            from remote_workbench.remote_directory import reject_managed_copy
            reject_managed_copy(posixpath.join(active_workspace.cwd, arguments['path']))
        if "host" in signature.parameters:
            arguments["host"] = active_workspace.host
        if "cwd" in signature.parameters:
            arguments["cwd"] = active_workspace.resolve_cwd(arguments.get("cwd"))
        token = workspace_executor.set((active_workspace.host, active_workspace.executor))
        try:
            claims = session.resources if session is not None else None
            if claims is not None and name == 'apply_patch' and arguments.get('edits'):
                from remote_workbench.edit_spec import prepared_edits
                if prepared_edits(arguments['edits']):
                    from remote_workbench.prepared_edit import normalize
                    normalize(arguments, active_workspace.executor)
            claim = None
            if claims is not None and name in {'exec_command', 'start_job', 'write_file', 'patch_file', 'apply_patch'} and not arguments.get('check_only'):
                declared = active_workspace.resolve_cwd(arguments.get('cwd'))
                root = posixpath.commonpath([active_workspace.cwd, declared])
                if isinstance(arguments.get('path'), str):
                    root = posixpath.commonpath([root, posixpath.dirname(posixpath.normpath(posixpath.join(declared, arguments['path'])))])
                if name == 'apply_patch' and arguments.get('expected_files'):
                    root = posixpath.commonpath([root, *[posixpath.dirname(p) for p in arguments['expected_files']]])
                claim = claims.acquire(active_workspace.executor.config.ssh_target, root, session.id, name)
            result = await function(**arguments)
            if claim:
                claims.finish(claim, result, background=name == 'start_job', shell=name == 'exec_command')
            elif claims is not None and name in {'job_status', 'job_wait', 'stop_job'}:
                claims.reconcile(session.id, arguments['job_id'], result)
            return result
        finally:
            workspace_executor.reset(token)

    invoke.__name__ = name
    invoke.__doc__ = description
    invoke.__signature__ = public_signature
    invoke.__annotations__ = {k: v for k, v in function.__annotations__.items() if k != "host"}
    invoke.__annotations__["return"] = dict[str, Any]
    return invoke


def _workspace_response(tool, default_max_bytes: int | None = None, *, viewer_url=None,
                        session=None, prepare_session=True):
    """Serialize a workspace tool result once, as compact JSON text.

    FastMCP's default structured-output mode sends dict results twice: once as
    pretty-printed text for older clients and once as structuredContent. Keep
    the text form for broad MCP client compatibility, but suppress the second
    copy and whitespace. The audit log remains the source for full output.
    """
    signature = inspect.signature(tool)
    if default_max_bytes is not None and "max_bytes" in signature.parameters:
        signature = signature.replace(parameters=[
            parameter.replace(default=default_max_bytes) if parameter.name == "max_bytes" else parameter
            for parameter in signature.parameters.values()
        ])

    @functools.wraps(tool)
    async def invoke(**kwargs):
        if default_max_bytes is not None:
            kwargs.setdefault("max_bytes", default_max_bytes)
        with audit.scoped_root(session.audit_dir) if session else nullcontext():
            try:
                if session is not None and prepare_session:
                    await session.ensure_ready()
                result = await tool(**kwargs)
            except Exception as exc:
                if viewer_url is None:
                    raise
                # Preserve MCP's error flag while also giving failed calls a
                # visible session record link. Cancellation still propagates.
                raise ToolError(f"{exc}\n\n[**Remote Workbench 操作记录**]({viewer_url})") from exc
            if viewer_url is not None:
                record_id = result.get("operation_id") or result.get("output_id") or result.get("id")
                link = viewer_url
                if (isinstance(record_id, str) and re.fullmatch(r"[a-f0-9]{32}", record_id)
                        and (audit.directory(record_id) / "record.json").is_file()):
                    link += "?id=" + record_id
                result["operation_log"] = f"[**Remote Workbench 操作记录**]({link})"
            if session is not None:
                result["session_id"] = session.id
            return json.dumps(result, ensure_ascii=False, separators=(",", ":"), default=str)

    invoke.__name__ = tool.__name__
    invoke.__doc__ = tool.__doc__
    invoke.__signature__ = signature.replace(return_annotation=str)
    invoke.__annotations__ = dict(getattr(tool, "__annotations__", {}))
    invoke.__annotations__["return"] = str
    return invoke


def create_server(workspace: Workspace, *, viewer_url: str | None = None, session=None) -> FastMCP:
    source_workspace = workspace
    if session is not None:
        workspace = session.workspace
    server = FastMCP("remote-workbench-workspace-" + workspace.name, instructions=(
        "Codex's native local tools always remain available and are never redirected by this MCP. "
        "Use this MCP only for operations the user explicitly assigns to its configured remote workspace "
        "in the current chat. Never infer remote intent "
        "from an existing SSH connection, previous calls, or another chat/session. "
        f"Configured remote workspace: {workspace.host}:{workspace.cwd}. "
        "workspace_info reports local metadata by default; set probe=true only when this chat "
        "explicitly needs to check or prepare the remote workspace. "
        "Selecting this remote workspace affects only Remote Workbench tools, not all project commands. "
        "Use these tools for remote files, commands, search, edits, Git and tests only after that explicit selection. "
        "Paths are remote; omitted/relative cwd starts at the workspace root. "
        "A cd/export in one call does not change subsequent calls. Explicitly activate "
        "the required environment in the same command. No automatic environment reuse. "
        "Control and audit records stay local; only shell/Python workers run remotely. "
        "For remote plots use fetch_image and show its image_markdown in chat. "
        "Do not read image bytes/base64 through text tools; use preview_path with native image viewing only for analysis. "
        "On SSH failure stop and report; never fall back to local project execution. "
        "This is a default directory, not a filesystem sandbox."
        + CALL_SOURCE_INSTRUCTIONS + REVIEW_INSTRUCTIONS
        + (f" Local audit viewer: {viewer_url}. Every tool result supplies operation_log. "
           "Show this link in a user-facing progress update before the first remote operation, then reuse it while work runs. "
           "Whenever you use these tools, include its Markdown link with the exact bold title "
           "'Remote Workbench 操作记录' in your user-facing update and final reply. "
           "Use the returned link directly; no separate workspace_info call is needed for it."
           if viewer_url else "")
        + (" Session records and jobs are separate; code stays in the configured actual directory. "
           if session is not None else "")
    ))

    async def workspace_info(probe: bool = False) -> dict[str, Any]:
        """Report local configuration metadata; probe SSH and validate the selected directory only when requested."""
        active_workspace = session.workspace if session is not None else workspace
        result = {"name": active_workspace.name, "host": active_workspace.host, "cwd": active_workspace.cwd,
                  "control": "local", "execution": "remote", "connected": None,
                  "ready": None, "probe_performed": False, "shell_state_reused": False,
                  "connection_message": "No remote connection was checked. Use probe=true only when this chat needs remote work."}
        if viewer_url is not None:
            result["viewer_url"] = viewer_url
        if session is not None:
            result.update(source_cwd=source_workspace.cwd, project_isolated=False,
                          directory_mode=session.directory_mode,
                          session_id=session.id, session_prepared=bool(session.prepared))
        if not probe:
            return result

        if session is not None:
            await session.ensure_ready()
            active_workspace = session.workspace
            result.update(cwd=active_workspace.cwd, session_prepared=bool(session.prepared))
        result["probe_performed"] = True
        connected, message = await active_workspace.executor.check_connection(force=True)
        result.update(connected=connected, connection_message=message)
        if connected:
            probe_code = ('import json,os; print(json.dumps({"cwd":os.getcwd(), '
                          '"shell":os.environ.get("REMOTE_WORKBENCH_LOGIN_SHELL", "")}))')
            probe = await active_workspace.executor.run_program(["python3", "-c", probe_code], output_budget=2048)
            result.update(ready=probe.exit_code == 0, probe=probe.stdout,
                          stderr=probe.stderr, output_id=probe.output_id)
            if probe.exit_code == 0:
                try:
                    detail = json.loads(probe.stdout)
                    result.update(shell=detail["shell"], probe=detail["cwd"] + "\n" + detail["shell"] + "\n")
                except (ValueError, KeyError, TypeError):
                    result.update(ready=False, stderr="Invalid remote shell probe")
        else:
            result["ready"] = False
        return result

    server.tool(structured_output=False)(_workspace_response(
        workspace_info, viewer_url=viewer_url, session=session, prepare_session=False))

    for name, function, description in TOOLS:
        default_max_bytes = {
            "exec_command": 2048,
            "read_file": 4096,
            "patch_file": 4096,
            "git_diff": 4096,
        }.get(name)
        tool = bind_tool(workspace, name, function, description,
                         workspace_provider=(lambda: session.workspace) if session is not None else None,
                         session=session)
        server.tool(structured_output=False)(_workspace_response(
            tool, default_max_bytes, viewer_url=viewer_url, session=session,
            prepare_session=name not in {"read_output", "search_output", "job_wait", "audit_get", "audit_list"}))
    return server


@contextmanager
def _local_audit_viewer(port: int | None, *, audit_dir=None, handler=None):
    """Optionally serve the audit viewer on loopback for this MCP lifetime."""
    if port is None:
        yield None
        return
    if isinstance(port, bool) or not 0 <= port <= 65535:
        raise ValueError("viewer port must be between 0 and 65535")

    handler = handler or (handler_for_directory(audit_dir) if audit_dir is not None else AuditViewerHandler)
    instance_id = uuid.uuid4().hex
    handler = handler_for_instance(handler, instance_id)
    try:
        viewer = ThreadingHTTPServer(("127.0.0.1", port), handler)
    except OSError as exc:
        if exc.errno != errno.EADDRINUSE:
            raise
        # MCP clients can start one process per chat or discovery request.
        # Keep each process's viewer independent of whoever owns this port.
        viewer = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        print(f"Audit viewer port {port} is in use; using {viewer.server_port}.",
              file=sys.stderr, flush=True)
    viewer.viewer_url = f"http://127.0.0.1:{viewer.server_port}/v/{instance_id}"
    thread = Thread(
        target=viewer.serve_forever,
        kwargs={"poll_interval": 0.1},
        name=f"remote-workbench-audit-viewer-{viewer.server_port}",
        daemon=True,
    )
    started = False
    try:
        thread.start()
        started = True
        yield viewer
    finally:
        try:
            if started:
                viewer.shutdown()
        finally:
            viewer.server_close()
            if started:
                thread.join()


def main():
    parser = argparse.ArgumentParser(description="Local Remote Workbench MCP bound to one remote workspace")
    parser.add_argument("--workspace", required=True, help="Local workspace JSON manifest")
    parser.add_argument("--viewer-port", type=int, default=0,
                        help="Local viewer port (default 0: OS-assigned); if occupied, use an available port")
    args = parser.parse_args()
    workspace = Workspace.load(args.workspace)
    from remote_workbench.workspace_session import WorkspaceSession
    session = WorkspaceSession(workspace)
    with _local_audit_viewer(args.viewer_port, audit_dir=session.audit_dir) as viewer:
        viewer_url = None
        if viewer is not None:
            viewer_url = viewer.viewer_url + "/"
            print(f"Local audit viewer: {viewer_url}", file=sys.stderr, flush=True)
        create_server(workspace, viewer_url=viewer_url, session=session).run()


if __name__ == "__main__":
    main()
