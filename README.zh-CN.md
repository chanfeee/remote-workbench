# Remote Workbench

[English](README.md) | **简体中文**

Remote Workbench 让本地 Agent 在明确选定的 SSH 服务器和工程目录中执行命令、读写文件。Agent、MCP 服务和操作记录留在本地；远端运行普通命令及通过 SSH 发送的小型 Python worker，无需安装 Codex CLI 或独立服务。

- 在本地与多台服务器之间切换，每个聊天独立保存绑定。
- 通过冲突检查读写文件，查看命令、输出和受管修改。
- 启动、查询、停止后台任务，并按原执行位置恢复。
- 用可选 HTML 页面查看本地记录，将远端图片拉取到聊天中展示。

## 快速开始

本机使用类 Unix 系统，需安装 Python 3.10+、OpenSSH 和 [uv](https://docs.astral.sh/uv/)。远端账号需要类 Unix 系统、Python 3.9+ 及 `fcntl`，且 SSH 命令中可调用 `python3`。Git 和补丁操作需要 Git。

### 1. 本机安装

以下从 `main` 安装开发源码。固定的日常安装见[开发与稳定版部署](docs/development.zh-CN.md)。

```sh
git clone --branch main https://github.com/chanfeee/remote-workbench.git
cd remote-workbench
uv sync --frozen
```

### 2. 配置服务器

```sh
mkdir -p ~/.config/remote-workbench
cp config.example.yaml ~/.config/remote-workbench/config.yaml
```

将文件中的 SSH 别名、账号和 ControlMaster socket 改为自己的配置。最小示例：

```yaml
hosts:
  myserver:
    ssh_host: myserver
    user: alice
    control_path: "~/.ssh/rwb-myserver"
    preamble: ""
```

`ssh_host` 可以使用已有 OpenSSH 配置中的别名。在 `hosts` 下增加条目即可配置多台服务器。socket 路径必须与建立连接时使用的路径相同；添加主机配置不会自动为聊天启用远端。

### 3. 在自己的终端认证

可以复用已有 ControlMaster；没有时由你手动建立：

```sh
uv run remote-workbench establish myserver --config ~/.config/remote-workbench/config.yaml
```

直接在 SSH 中完成密码、Kerberos 或 OTP 认证，并保持终端连接。MCP 只复用已建立的连接，不读取密码文件，也不自动重新认证。

### 4. 只注册一个 MCP

在 Codex 中使用 Session 入口，将下面的路径全部替换为本机绝对路径：

```toml
[mcp_servers.remote-workbench]
command = "/absolute/path/remote-workbench/.venv/bin/remote-workbench-session"
args = [
  "--config", "/absolute/path/.config/remote-workbench/config.yaml",
  "--data-dir", "/absolute/path/remote-workbench-state"
]
```

修改配置后重载客户端。只注册一个 `remote-workbench`；测试其他安装时替换同一项的启动器。此入口不需要 `workspace.json`。

Session 路由依赖客户端提供 `_meta.threadId`；缺少可用的聊天身份时拒绝远程执行。`workspace_info` 可查看当前绑定，不会建立 SSH 连接。

## 本地与多服务器协作

可以直接向 Agent 描述任务：

> 先在本地运行检查，再到 myserver 的 /home/alice/project 检查代码并运行一个小型测试。显示实际执行目录和操作记录。

- 新聊天默认本地，本地工作使用原生本地工具。
- `workspace_switch(host, cwd)` 为当前聊天选择远程目标。检查回复中的实际 `cwd`；目录行为取决于下文说明的安装版本。
- `workspace_use_local()` 清除活动远程绑定，保留记录和已有任务。
- 切换目标不改变旧任务的原服务器、目录和运行状态位置。远程请求失败时不会悄悄改为本地执行。

## 工作目录

`source_cwd` 表示选定的来源，`cwd` 表示命令实际执行的位置。

`0.4.0` 源码直接使用规范化后的实际目录，不再提供工程副本模式、交付接口或旧副本任务恢复。已有目录和本地记录不删除，工作时重新选择实际目录。旧 stable 安装在单独升级前保持原有行为。

聊天独立指绑定和记录独立；direct 模式允许多个聊天共享代码，运行状态放在代码目录之外。协作写入协调覆盖使用同一本地状态目录的新版 MCP 进程及重叠的选定目录，不隔离外部终端、其他控制端或选定目录之外的任意写入。范围与恢复方式见[目录模式契约](docs/workspace-mode-contract.zh-CN.md)。

**本地未发布修复：** 工作区、build 和 run 目录无需是 Git 仓库。快照编辑与文件编辑不依赖 Git，仍在本地操作记录中保留每次修改的 diff；只有 Git 状态/差异查询需要仓库。绑定目录不探测 Git，也不初始化仓库。

## 命令、记录与图片

工具回复提供有界输出和 **Remote Workbench 操作记录** 链接，供 Agent 在聊天中展示。完整命令输出和 diff 保留在本地记录中，需要时通过 `read_output` 分页读取。

可选 HTML 查看器只读，默认自动刷新。每个进程使用可用的本机回环端口和独立 URL 前缀，可与其他本地页面并存。MCP 重启后使用新回复中的链接；关闭页面不影响执行，也不会删除记录。

受管编辑记录本次修改。Git diff 是显式获取的当前差异；操作记录不会追踪任意 shell 命令或外部终端造成的所有文件写入。

查看图片时，可以让 Agent 展示远端 PNG/JPG 或 PDF 的指定页。`fetch_image` 保存本地副本并返回聊天预览引用，不将 base64 放入 MCP 文本。PDF/SVG 预览需要可选的本地渲染包：

```sh
uv sync --frozen --extra images
```

详见[聊天内看图](docs/images.zh-CN.md)和[后台任务恢复](docs/lifecycle-recovery.zh-CN.md)。Agent 或 MCP 退出后仍可通过终端检查服务器；`remote-workbench-cleanup` 提供独立的停止入口，使用任务的原主机、job ID 和运行状态目录。

## 执行边界

- 每条命令启动远端账号选定的默认登录 shell，不要求 zsh。Conda、venv 等环境需要在每次相关命令中激活；命令通道不分配 PTY。
- 受管编辑保留内容哈希、补丁上下文检查和原子写入。普通执行不自动扫描项目内容，也不维护项目测试证据缓存。
- 取消和期限管理通过监督器清理任务进程组。无法确认的状态保留未知；发出取消请求不代表所有后代都已停止。大量计算前，Agent 必须展示命令、并发规模和环境，并得到用户确认。详见[任务生命周期与计算审核](docs/process-safety.zh-CN.md)。
- 命令使用远端账号的权限，工作区协调不是操作系统沙箱。本地审计文件可能包含命令、文件内容和输出，应私有保存。
- 退出码和测试摘要描述观测到的执行结果；验证是否充分由 Agent 和用户判断。token 与费用工具仅用于手动、离线基准，不接入日常执行和 UI。

## 开发版与稳定发行

| 渠道 | 用途 |
| --- | --- |
| `main` | 开发主线；PR 通过 CI 后由维护者手动合并。 |
| `stable` | 从 `main` 推广并验收的源码候选。 |
| 版本 tag / GitHub Release | 固定发行，用于日常安装和回滚。 |

`develop` 在迁移期保留。分支更新不会替换独立安装的稳定版，也不会重载已运行的客户端。

上文的 direct 改动、快照编辑、结构化命令参数、统一的公开工具接口已纳入 `0.4.0` 源码。主编辑入口传快照 ID 和新内容行数组，不手抄旧上下文或补丁标记；顺序 argv 批量检查共用一次受监督 SSH；显式“绑定/检查/指定文件读取”和“修改/后续命令”组合还可减少模型往返，分别返回修改与命令事实。本地暂存文件仍是显式兼容选项，详见[编辑与调用边界](docs/tool-call-syntax.zh-CN.md)。安装与推广方式见[部署说明](docs/development.zh-CN.md)和[发行规则](docs/release-policy.zh-CN.md)。

已有的 `remote-workbench-workspace`、`remote-workbench-ssh` 安装继续作为兼容入口保留。本地开发版固定入口也默认使用原目录；工作、代码/环境和结果位置可以分别指定，session只隔离记录。详见[Session记录与实际目录](docs/sessions.zh-CN.md)。早期 Onirika 配置不会自动迁移。

## 文档索引

| 主题 | 入口 |
| --- | --- |
| 主机配置 | [config.example.yaml](config.example.yaml) |
| 安装与发行流程 | [部署说明](docs/development.zh-CN.md) · [发行规则](docs/release-policy.zh-CN.md) |
| 目录模式与协调 | [模式契约](docs/workspace-mode-contract.zh-CN.md) |
| 后台任务与恢复 | [恢复说明](docs/lifecycle-recovery.zh-CN.md) · [进程安全](docs/process-safety.zh-CN.md) |
| 远端图片 | [聊天预览](docs/images.zh-CN.md) |
| 开发状态与验证 | [开发计划](docs/development-plan.zh-CN.md) · [验证记录](VALIDATION.md) |
| 手动 token / 费用实验 | [离线基准](docs/token-budget.zh-CN.md) |

## 许可证

Remote Workbench 是基于 [Onirika](https://github.com/xinyue-uoft/Onirika) 独立维护的衍生项目，采用 GNU AGPL 第 3 版。详见 [LICENSE](LICENSE)、[UPSTREAM.md](UPSTREAM.md) 及保留的[上游 README](README.upstream.md)。

以修改后的版本提供网络服务时，应按 AGPLv3 要求提供对应源码。
