# Remote Workbench

**English** | [简体中文](README.zh-CN.md)

Remote Workbench gives a local agent command and file access to an explicitly selected SSH server and project directory. The agent, MCP server, and operation records stay local. The remote side runs ordinary commands and a small Python worker sent over SSH; it needs no Codex CLI or separately installed service.

- Switch between local work and several remote servers, with independent bindings for each chat.
- Read and edit files with conflict checks; inspect commands, output, and managed changes.
- Start, query, stop, and recover background jobs using their original execution locations.
- View local records in an optional HTML page and fetch remote images for display in chat.

## Quickstart

Use a Unix-like local system with Python 3.10+, OpenSSH, and [uv](https://docs.astral.sh/uv/). The remote account needs a Unix-like system with Python 3.9+ and `fcntl`; `python3` must be available to SSH commands. Git is required for Git and patch operations.

### 1. Install locally

The following installs development source from `main`. For a fixed daily installation, see [development and stable deployment](docs/development.zh-CN.md).

```sh
git clone --branch main https://github.com/chanfeee/remote-workbench.git
cd remote-workbench
uv sync --frozen
```

### 2. Configure a server

```sh
mkdir -p ~/.config/remote-workbench
cp config.example.yaml ~/.config/remote-workbench/config.yaml
```

Edit the file with your own SSH alias, account, and ControlMaster socket. A minimal example:

```yaml
hosts:
  myserver:
    ssh_host: myserver
    user: alice
    control_path: "~/.ssh/rwb-myserver"
    preamble: ""
```

`ssh_host` may be an alias from your existing OpenSSH configuration. Add more entries under `hosts` for other servers. The socket path must match the one used to establish the connection; configuring a host does not activate it for a chat.

### 3. Authenticate in your terminal

Reuse an existing ControlMaster, or establish one yourself:

```sh
uv run remote-workbench establish myserver --config ~/.config/remote-workbench/config.yaml
```

Complete password, Kerberos, or OTP prompts directly in SSH and keep the terminal connection open. MCP only reuses the established connection; it does not read password files or reauthenticate automatically.

### 4. Register one MCP

For Codex, use the Session entry point and replace all paths below with absolute local paths:

```toml
[mcp_servers.remote-workbench]
command = "/absolute/path/remote-workbench/.venv/bin/remote-workbench-session"
args = [
  "--config", "/absolute/path/.config/remote-workbench/config.yaml",
  "--data-dir", "/absolute/path/remote-workbench-state"
]
```

Reload the client after changing its configuration. Register only one `remote-workbench` entry; switch its launcher when testing another installation. This entry does not require `workspace.json`.

Session routing requires a client-provided `_meta.threadId`. Without a usable chat identity, remote execution is rejected. `workspace_info` reports the current binding without opening SSH.

## Work locally and across servers

A request can be as simple as:

> Run checks locally, then use myserver at /home/alice/project to inspect the code and run a small test. Show the actual execution directory and operation record.

- New chats start locally. Use native local tools for local work.
- `workspace_switch(host, cwd)` selects a remote target for this chat. Check the actual `cwd` in its reply; directory behavior depends on the installed version below.
- `workspace_use_local()` clears the active remote binding and preserves records and existing jobs.
- Switching targets leaves old jobs attached to their original server, directory, and runtime state. A failed remote request is not silently run locally.

## Working directories

`source_cwd` identifies the selected source; `cwd` is where commands actually run.

The `0.3.0a15` development source binds the normalized actual directory. There is no project-copy mode or copy-delivery API. Old copy bindings and jobs are unsupported; existing files and local records are left untouched. Select the actual directory to work. Older stable installations retain their installed behavior until separately upgraded.

Chat isolation applies to bindings and records; direct mode allows chats to share code. Its runtime state lives outside the code directory. Cooperative write coordination covers overlapping selected directories for updated MCP processes sharing one local state directory. It does not isolate external terminals, other controllers, or arbitrary writes outside the selected directory. See the [directory mode contract](docs/workspace-mode-contract.zh-CN.md) for scope and recovery.

## Commands, records, and images

Tool replies include bounded output and a **Remote Workbench 操作记录** link for the agent to show in chat. Full command output and diffs remain in local records and can be read in pages with `read_output`.

The optional HTML viewer is read-only and refreshes automatically. Each process has an available loopback port and its own URL prefix, so it can coexist with other local pages. Use a fresh returned link after restarting MCP. Closing the page does not stop execution or remove records.

Managed edits record their own changes. Git diff is an explicitly requested view of current differences; the record does not track every file written by arbitrary shell commands or external terminals.

For images, ask the agent to show a remote PNG/JPG or a selected PDF page. `fetch_image` saves a local copy and returns a chat preview reference without putting base64 in MCP text. PDF/SVG previews need the optional local rendering packages:

```sh
uv sync --frozen --extra images
```

See [images in chat](docs/images.zh-CN.md) and [job recovery](docs/lifecycle-recovery.zh-CN.md). If the agent or MCP has exited, the terminal can still be used to inspect the server; `remote-workbench-cleanup` provides a separate stop entry point using the original host, job ID, and runtime-state directory.

## Execution boundaries

- Commands use fresh login shells selected from the remote account; zsh is not required. Activate Conda, venv, or other environments in each command that needs them. The command channel does not allocate a PTY.
- Managed edits retain content-hash checks, patch-context checks, and atomic writes. Ordinary execution does not automatically scan project contents or maintain project test-evidence caches.
- Cancellation and deadlines use supervised process-group cleanup. Unknown status remains unknown; a cancellation request does not by itself prove that every descendant stopped. Before substantial computation, the agent must show the command, concurrency, and environment and obtain user confirmation. See [task lifecycle and compute review](docs/process-safety.zh-CN.md).
- Commands have the permissions of the remote account. Workspace coordination is not an operating-system sandbox. Local audit files can contain commands, file contents, and output; keep them private.
- Exit codes and test summaries describe observed execution. The agent and user determine whether validation is sufficient. Token and cost tools are manual, offline benchmarks, separate from daily execution and UI.

## Development and stable releases

| Channel | Purpose |
| --- | --- |
| `main` | Development integration; maintainers merge PRs manually after CI passes. |
| `stable` | Validated source candidates promoted from `main`. |
| Version tags / GitHub Releases | Fixed distributions for daily installations and rollback. |

`develop` is retained during migration. Branch updates do not replace an independently installed stable version or reload a running client.

The direct-mode changes above, snapshot-backed edits, structured command arguments, and unified advertised tool interface are included in the `0.3.0a15` development source. Prepared edits send a snapshot ID plus new lines, avoiding copied old context and handwritten patch markers. Sequential argv batches combine checks in one supervised SSH call. Explicit switch/check/read and edit/check combinations also reduce client round trips, while returning separate edit and command outcomes. A local staging file remains an explicit compatibility option. See [editing and call boundaries](docs/tool-call-syntax.zh-CN.md). For installation and promotion, see [deployment](docs/development.zh-CN.md) and [release policy](docs/release-policy.zh-CN.md) for installation and promotion.

Existing `remote-workbench-workspace` and `remote-workbench-ssh` installations remain compatibility entry points. The local development fixed-workspace entry also defaults to the original directory. Code, environment and result locations may be independent; session separation applies to records. See [session records and actual directories](docs/sessions.zh-CN.md). Earlier Onirika configuration is not migrated automatically.

## Documentation

| Topic | Reference |
| --- | --- |
| Host configuration | [config.example.yaml](config.example.yaml) |
| Installations and release process | [Deployment](docs/development.zh-CN.md) · [Release policy](docs/release-policy.zh-CN.md) |
| Directory modes and coordination | [Mode contract](docs/workspace-mode-contract.zh-CN.md) |
| Background jobs and recovery | [Recovery](docs/lifecycle-recovery.zh-CN.md) · [Process safety](docs/process-safety.zh-CN.md) |
| Remote images | [Chat previews](docs/images.zh-CN.md) |
| Development status and verification | [Plan](docs/development-plan.zh-CN.md) · [Validation records](VALIDATION.md) |
| Manual token/cost experiments | [Offline benchmarks](docs/token-budget.zh-CN.md) |

## License

Remote Workbench is an independently maintained derivative of [Onirika](https://github.com/xinyue-uoft/Onirika), distributed under GNU AGPL version 3. See [LICENSE](LICENSE), [UPSTREAM.md](UPSTREAM.md), and the preserved [upstream README](README.upstream.md).

When operating a modified version as a network service, provide the corresponding source as required by AGPLv3.
