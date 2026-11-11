# 开发版与稳定版

采用 `main` 开发主线、`stable` 稳定候选分支和固定版本 Release，具体合并/发行规则见[分支与发行](release-policy.zh-CN.md)。PR 通过 CI 后由维护者手动合并，不启用自动合并。

## 开发安装

在独立源码 checkout 中跟随 main：

```sh
git clone --branch main https://github.com/chanfeee/remote-workbench.git "$HOME/opt/remote-workbench/dev"
cd "$HOME/opt/remote-workbench/dev"
uv sync --frozen --all-extras
```

功能开发从 main 创建短分支，完成测试后向 main 提 PR。现有 develop 分支和未提交工作不自动重置、删除或搬移；先完成审阅与迁移，再切换日常开发 checkout。旧 `.local/develop/` 是安装目录名称，可以保留，不代表今后还维护另一条长期开发主线。

Node 仅用于开发测试，无 npm 依赖；MCP 和远端运行继续使用 Python。开发安装可 editable，配置、状态和审计应独立于稳定安装。

共享操作的审计与执行器选择位于 `tool_runtime.py`，工具函数不注册 MCP。旧 SSH 入口在 `server.py` 中显式注册；固定工作区与 session 直接复用这些函数。`session_server.py` 负责身份、绑定和工具 schema，`session_operations.py` 负责执行、编辑后续检查和作业提交。新增能力应沿用这些边界，避免从共享操作反向导入服务入口。

默认安装的 SSH MCP 使用原生文本回复，旧卡片资源仅作按需兼容。`proxy` 和 `web` 分别用于代理网关和独立 Web/PTY 启动器；上面的 `--all-extras` 为完整本地回归安装全部可选依赖。离线费用与旧测试证据工具保存在 `scripts/offline/`，随源码包提供，不进入运行时 wheel。

## 稳定安装

stable 是移动的源码分支；正式使用固定 tag/Release 的 wheel，避免分支变化影响运行中的 MCP。每版创建独立目录，例如：

```text
~/opt/remote-workbench/stable/
  config/hosts.yaml
  session-data/
  releases/VERSION-COMMIT/
    dist/
    runtime-requirements.txt
    .venv/
```

从 GitHub Release 下载 wheel、runtime-requirements.txt 和 SHA256SUMS，核对 SHA256 后安装。将路径和文件名替换为实际发行资产：

```sh
release_dir="$HOME/opt/remote-workbench/stable/releases/VERSION-COMMIT"
uv venv --python 3.13 "$release_dir/.venv"
uv pip install --python "$release_dir/.venv/bin/python" \
  --constraints "$release_dir/runtime-requirements.txt" \
  "$release_dir/dist/remote_workbench-VERSION-py3-none-any.whl"
"$release_dir/.venv/bin/python" -I -c 'import importlib.metadata, remote_workbench; print(importlib.metadata.version("remote-workbench")); print(remote_workbench.__file__)'
```

记录完整 commit、wheel SHA256、测试及实际客户端加载版本。稳定版不使用 `uv sync` / editable 安装，更新后保留旧 release 目录；回滚只切换启动器，不清理旧代码、日志或任务。

### 更新已有的本地 stable 安装

已有 `config/hosts.yaml` 且启动器通过 `current/venv/bin/remote-workbench-session` 加载时，可以在源码仓库中运行（需要 Python 3.11+ 和 uv）：

```sh
python3 scripts/update_local_stable.py
```

默认安装本地 `stable` 分支，目标为仓库内 `.local/stable`。其他位置使用 `--install-root /absolute/stable`，也可用 `--ref` 明确选择已存在的提交或本地 ref。脚本不 fetch、不移动分支、不读取未提交源码；先更新本地 stable，再运行此命令。

更新会归档确定提交，构建 wheel，按该提交的锁文件安装依赖，并验证独立安装的导入、MCP stdio 的原生文本默认、历史资源读取和文件协调能力，不启动独立 HTML 查看器。只有验证成功才原子切换 `current`；配置和历史状态保留。重复安装同一提交仅复核现有安装，不重新构建。失败候选保留供检查，旧入口保持原样。`release.json` 保存来源 commit、wheel SHA256 和前一个安装位置，回滚时可将 `current` 重新指向该位置。

这是本地部署便利入口，不会创建 tag 或 GitHub Release，也不改变正式发行流程。更新后仍需重载 MCP；脚本不会中断现有聊天或其任务。

## 一个 MCP，两个安装渠道

Codex 只注册一个 `remote-workbench`。日常指向独立 stable 启动器，开发测试时替换同一项的 command 指向开发 checkout；不同时增加两套 MCP，不设置 full/compact 功能模式。

```toml
[mcp_servers.remote-workbench]
command = "/absolute/path/to/stable/bin/remote-workbench-session"
args = []
enabled = true
```

启动器固定该安装的 Python，并明确传入对应 `--config` 和 `--data-dir`；旧 `--viewer-port 0` 参数仍可解析，但不再启动服务。更换安装后重载客户端并核对实际版本；改变源码或 symlink 不会自动更新已运行进程。

不同项目的 MCP 进程可能同时加载不同安装。项目内 `.codex/config.toml` 的候选入口也应在推广时检查，不能只重载开发项目。通过 `workspace_info` 核对 `runtime.version`、`runtime.package_path`、`runtime.data_dir` 和 `write_coordination`；`0.4.2` 的协调值为 `managed-files`。旧进程继续报目录占用时，应在对应项目重载其 MCP，不删除活动任务或历史占用记录来绕过问题。重载前先保存正在运行的前台工作；已有后台任务按原 job 身份恢复。

新聊天默认本地，只有明确选择服务器/目录后 MCP 才执行远程任务。本地原生工具保持本地；复用人工建立的 ControlMaster，不处理密码或 OTP。开发与稳定安装分别保留状态，但不同聊天的任务依然按原始目标恢复，不因分支或安装渠道切换而重跑命令。

当前新操作使用原生文本和独立 CLI 查看，旧卡片只保留按需兼容，不依赖本机网页端口。旧文档中的 copy/session 原型与 a7 查看器交接是历史记录，保留于 [Session 原型](session-prototype.zh-CN.md)、[旧 Develop 交接](develop-handoff.zh-CN.md)；实际目录行为以所安装版本及[目录契约](workspace-mode-contract.zh-CN.md)为准。

## 发布内容边界

真实服务器的账号、节点、工作目录、会话ID、操作日志、截图和个人用量报告只保存在仓库外或被忽略的本地运行目录。公开验证文档只保留通用接口契约、自动化测试覆盖和必要限制，不附个人部署的执行流程。

个人报告目录不提交；源码包对报告和本地运行目录另设显式排除规则。发行前同时检查Git候选文件、wheel和sdist，不能只依赖忽略规则。测试夹具使用虚构主机与账号，项目官方地址及上游许可证署名保持不变。
