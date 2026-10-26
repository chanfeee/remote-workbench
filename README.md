# Remote Workbench

Remote Workbench keeps the agent and MCP control process on your local machine while using a remote SSH project as its working environment. The remote side runs ordinary shell commands and a small Python worker sent over SSH; it does not need Codex CLI or a separately installed service.

This is an independently maintained derivative of [Onirika](https://github.com/xinyue-uoft/Onirika), released under GNU AGPL version 3. See [UPSTREAM.md](UPSTREAM.md) for provenance and [LICENSE](LICENSE) for the full license text.

中文使用步骤见 [快速上手](docs/quickstart.zh-CN.md)。

本机开发环境与日常稳定版的隔离、安装和切换方式见[开发与稳定版部署](docs/development.zh-CN.md)。

Develop 的精简工具模式照常可用；Token/credits 计量仅用于手动运行的独立、可重复基准样本。Remote Workbench 自身的日常命令、编辑和测试路径不采集 token/credits 或模型费用，也不为计量增加模型调用；日常工作流不在回复或 UI 中追加用量评估，基准门槛也不阻止普通任务完成。上述约定仅限定 Remote Workbench 自身的执行与集成路径，不声明 Codex App 或模型服务如何记录账户用量。离线费用账本和门槛脚本仅作为测试辅助工具保留；完整操作记录与必要执行诊断照常保留。基准口径与历史样本见[独立 Token 基准](docs/token-budget.zh-CN.md)。

第一批可靠性修复、离线费用账本测试工具及后续目录模式设计见[开发任务与执行状态](docs/development-plan.zh-CN.md)和[direct/copy 契约](docs/workspace-mode-contract.zh-CN.md)。当前 develop 阶段的 MCP 记录明确目标上的命令、文件与后台任务生命周期；科研解释、验证是否充分及被操作项目的测试适用性由调用 Agent 和用户判断。费用脚本为 `python3 scripts/build_cost_ledger.py --help`，仅用于手动基准样本；费率或速度缺失时保留未知，不当作实际零费用。

The `main` branch is the development integration branch: pull requests are merged manually after CI passes. The `stable` branch tracks validated release candidates; version tags and GitHub Releases pin each stable distribution. Existing `develop` work is retained during migration. See [branch and release policy](docs/release-policy.zh-CN.md).

The `remote-workbench-session` entry point (0.3.0a10) provides: persistent workspaces routed by MCP `_meta.threadId`, atomic host/directory switching, jobs pinned to their original targets and checked against node/process identity, explicit project snapshots, and measured response bytes. Each thread keeps its own remote project copies. An identity-only diagnostic is available before enabling remote tools. This does not change an independently installed stable release; see [Session 原型与客户端联调边界](docs/session-prototype.zh-CN.md).

当前版本提供任务重试去重、独立控制通道、后台日志自动采集、本地等待与搜索、工程基线及交付预览，见[可靠性与权限边界](docs/reliability.zh-CN.md)。stable 只在独立验证后手动推广固定构建；凭证隔离和真实集群验收仍需单独完成。

Develop 的后台任务恢复与操作记录见[后台任务恢复](docs/lifecycle-recovery.zh-CN.md)：`workspace_resume()` 默认只返回 binding、jobs 和 note 操作事实，不读写测试缓存；`include_legacy_test_history=True` 只读旧测试历史，不判断其对当前代码是否适用，也不触发 SSH 扫描。`workspace_snapshot()` 默认检查显式元数据和 Git 状态；只有显式启用有界内容扫描时才计算内容指纹，不自动维护项目验证结论。丢失启动回复或重启 MCP 后仍可接回原任务。

## Work locally and across several servers

Register one MCP named `remote-workbench`. Stable/develop are installation channels; switching channels replaces this entry’s launcher instead of adding another MCP. Routine identity checks use `workspace_info`.

For mixed local and remote projects, use `remote-workbench-session`. Each new chat starts in local mode; tool discovery and `workspace_info` do not contact SSH. Use native local tools for local commands, choose a remote host and source directory with `workspace_switch`, and use `workspace_use_local` to clear that chat's active remote binding. Choosing a server does not redirect all project commands. Existing jobs stay attached to their original server when you switch, and other chats keep independent targets. See [本地与多服务器协作](docs/reliability.zh-CN.md#同一项目的本地与多服务器协作).

## Use a remote project as the default environment

The fixed-workspace entry point, `remote-workbench-workspace`, binds one local MCP process to a host and an absolute remote template directory. Its tools omit the host parameter and resolve relative paths from that process's private project copy. Copy [workspace.example.json](workspace.example.json), then set `host` to an alias from your local SSH configuration and `cwd` to the remote project template.

The workspace starts the remote account's configured login shell for each call, using that shell's own login and interactive startup files. No particular shell, including zsh, is required. Foreground commands and detached jobs use the same selection. Write commands and any preamble in the remote shell's syntax. A directory change, environment variable, or activated Conda/venv environment does not persist into the next call; activate the environment in each command that needs it.

The tool uses a byte-preserving SSH command channel without a PTY. Startup configuration is loaded, but terminal-dependent startup branches and job-control behavior can differ from a manual SSH terminal session. `workspace_info` reports the selected shell and project directory so the agent can verify the connection before editing.

The workspace is a convenient default directory, not a filesystem sandbox: arbitrary shell commands can access anything the remote account can access.

## Sessions and operation records

From version 0.2.3, each workspace MCP stdio process creates a fresh UUID session at startup. Its local audit records, saved outputs, and viewer are isolated from other process sessions. The first remote tool call creates a separate project copy alongside the configured source directory; the configured project remains a template and is not edited by workspace tools. Restarting the MCP creates a new session and project copy while retaining earlier copies and records. Git copies include the current working tree, including uncommitted changes and non-ignored untracked files; ignored dependencies, environment directories, and caches are left out. See [Session isolation and operation records](docs/sessions.zh-CN.md) for the path layout and client boundary.

Every workspace JSON tool result includes its `session_id` and an `operation_log` Markdown link; MCP tool exceptions retain their error flag and include the link in the error text. MCP instructions ask the agent to include the supplied link, with the exact visible title **Remote Workbench 操作记录**, in its reply after using a tool. The link points to that session's viewer and operation. Viewers use an OS-assigned local port by default and a per-process URL prefix; if a requested port is occupied, the viewer selects another port without using or stopping the service already there. Reopen a fresh operation link after restarting the MCP; old viewer URLs are not permanent. This is an agent-facing default implemented through MCP instructions and returned data, not a way for the server to force Codex to render a native terminal or file-change card. No extra `workspace_info` call is needed just to obtain the operation-record link.

## Install and configure

Requirements are Python 3.10+ and [uv](https://docs.astral.sh/uv/). The remote account needs a Unix-like system with Python 3.9+ and `fcntl`; `python3` must be available to SSH command sessions. Git is needed for Git status, diffs, and patch tools. SSH authentication remains with your local SSH setup and an existing ControlMaster connection.

```sh
git clone https://github.com/chanfeee/remote-workbench.git
cd remote-workbench
uv sync --locked --extra dev
```

Copy `config.example.yaml` to `~/.config/remote-workbench/config.yaml` and set your host alias, SSH destination, account, ControlMaster path, and default directory. Its `preamble` is empty by default. Use a preamble only when you intentionally want it run before every command.

```sh
mkdir -p ~/.config/remote-workbench
cp config.example.yaml ~/.config/remote-workbench/config.yaml
cp workspace.example.json workspace.json
uv run remote-workbench establish myserver
```

Edit `workspace.json` with the actual SSH alias and remote project path before using the workspace MCP entry.

`REMOTE_WORKBENCH_CONFIG` or the `--config` option on `remote-workbench-ssh` can select another local YAML file. The workspace manifest can also name a config file with its optional `config` field. Existing Onirika settings are not discovered or migrated automatically; review and copy the host settings you want into the new config.

## Connect an MCP client

Update the paths in [codex-mcp.example.toml](codex-mcp.example.toml) for your clone and workspace manifest, then add the MCP entry to your local client's configuration. After `uv sync`, the environment and entry points exist under the checkout. Configure one of these modes for a project:

- `remote-workbench-workspace` — one host and project template, recommended for remote-first coding. It serves the session's local audit viewer on loopback by default while the MCP process runs; the OS assigns an unused port unless `--viewer-port` specifies a preference.
- `remote-workbench-ssh` — the multi-host MCP with an explicit `host` argument on its tools.
- `remote-workbench-audit` — serve the local read-only audit viewer separately.

`remote-workbench` is the command dispatcher for setup, SSH connection, gateway, and other bundled commands. The MCP tools do not execute Codex CLI remotely.

## Commands, output, and code changes

The local audit viewer presents the actual remote command, its output, and file diffs in a readable page. Automatic refresh is enabled on opening, updates every two seconds, and can be turned off. Each MCP process gets an OS-assigned loopback port and a random URL prefix, keeping its page separate from other projects' local HTML services. You may pass `--viewer-port` to prefer a specific port; if it is occupied, Remote Workbench selects another available port and leaves the existing service alone. Tool results contain the actual `operation_log` link, and `workspace_info.viewer_url` and startup stderr report the current viewer URL. The page also checks its process identity on refresh, so an old open tab cannot silently display another viewer's records if a port is later reused. After restarting the MCP, use a new operation link; old URLs expire. The page reads local audit files directly without sending them back through MCP. The viewer is a separate page: it does not turn ordinary MCP calls into Codex's native terminal or file-change cards. If an agent explicitly reads saved output through MCP, that content still uses model context.

Remote plots can be displayed in chat with `fetch_image`: it saves a private local copy and returns `image_markdown`, without sending image/base64 data through MCP text. PNG/JPG display directly; the optional local `images` extra provides PNG previews for a selected PDF page or a self-contained SVG. The HTML viewer is unchanged. See [聊天内看图](docs/images.zh-CN.md).

The bound workspace returns compact previews by default: 2 KiB for command output and 4 KiB for file, patch, and Git diff output. Full output and diffs remain in local audit records and can be read in pages when needed. MCP responses use one compact JSON text result rather than duplicating it in text and structured content.

File edits can use hash-guarded replacements or unified Git patches. Review the returned change with `git_diff` or `git_status`, then run the relevant checks on the remote host. Remote jobs run only when explicitly started.

Develop adds supervised process-group cleanup for cancellation, disconnects and deadlines, plus `stop_job` and a control-only `remote-workbench-cleanup` entry point. Substantial computation requires the agent to present the command, concurrency and environment and obtain explicit user confirmation; common parallel launchers have a review check. This workflow requires no cgroup or universal process cap. It is not an OS sandbox or a guarantee against runaway processes. See [计算审核与任务生命周期](docs/process-safety.zh-CN.md).

Local audit files can contain commands, arguments, file contents, and output. Keep them private. Set `REMOTE_WORKBENCH_AUDIT_DIR` to choose their local directory; the default directory name is `.remote-workbench-audit`. In a bound workspace, remote lock and job state is stored beneath the workspace root in `.remote-workbench-state`; `REMOTE_WORKBENCH_REMOTE_STATE_DIR` controls the remote state directory for other modes.

## Configuration migration

The renamed project uses the `remote_workbench` Python package, `remote-workbench` command names, `REMOTE_WORKBENCH_*` environment variables, and `~/.config/remote-workbench/config.yaml`. It does not load the previous `onirika` configuration path by default. To move from an earlier installation, copy only the host settings you still need, check the ControlMaster socket path, and update your MCP command. Keep the previous installation available until you have verified the new one.

## License and network use

Remote Workbench is distributed under GNU AGPL version 3, as a modified work based on Onirika. If you operate a modified version as a network service, make the corresponding source for the version users interact with available as required by AGPLv3. See [LICENSE](LICENSE).
