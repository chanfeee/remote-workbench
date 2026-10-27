# 实际目录工作区契约

2026-10-26：本轮实现 direct；历史设计见 Git 历史。本文件描述实际支持范围，验证层次见 [验收记录](validation/direct-workspace.zh-CN.md)。独立 stable 安装和 main 不随本地开发改动升级。

## 选择目录

`remote-workbench-session` 的 `workspace_switch(host, cwd)`：

- 远端 `realpath` 规范化后直接使用该目录，不创建代码副本，不采集初始项目内容基线。只规范化目录并获取运行状态所需的 home，不探测 Git，也不扫描项目内容。
- 不再接受 `directory_mode` 参数，不再提供 copy 模式或副本交付接口。
- 新聊天默认本地。切换目录只影响本 MCP，原生本地工具保持本地。
- `source_cwd` 与 `cwd` 均指选定的实际目录；`directory_mode="direct"` 和 `project_isolated=false` 只作为结果事实保留。

聊天绑定、审计、输出和任务归属仍独立；代码目录不必独立。可以在同仓不同子目录、不同仓库，或显式共享相同目录工作。

## 状态和任务归属

支持的目标保存在 `direct_targets`，包含不可变的远端运行状态路径。旧副本的绑定、任务和交付不再兼容；遇到旧绑定应重新选择实际目录。MCP 不迁移、重建、交付或删除旧副本，也不删除已有本地历史数据。旧任务若仍在运行，需在原服务器终端人工接管，不能通过新任务重跑来“恢复”。

运行状态位于远端 `$HOME/.local/state/remote-workbench/targets/<target_id>`。不同聊天的运行状态独立，不在选定工程中创建副本或 `.remote-workbench-state`。后台 job origin 固定原 target、cwd、运行状态和 SSH endpoint；切换服务器或目录不会改变这些位置，恢复不重跑命令。

固定工作区入口 `remote-workbench-workspace` 同样使用实际目录，记录按进程 session 分离，远端运行状态保存在用户状态目录的独立 session 位置。

## 第一版目录协调

适用：同一本地控制端、同一 data-dir、全部使用本轮更新版本的 session MCP、配置解析到同一 SSH endpoint 的 direct 目标。

资源键使用 endpoint 与规范化的**选定目录**，不是 Git 仓库根：`/repo/a` 与 `/repo/b` 可并行；`/repo` 与 `/repo/a` 冲突。direct 受管修改、普通 shell 和后台启动按潜在写入处理；补丁 check-only 不占写入资源。多个聊天可以绑定同一目录，写入冲突时返回占用信息供轮流执行，不暗中换目录。

后台占用不在启动回复时释放。占用持久化到本地 `resources.sqlite3`，本地进程退出不代表远端停止。已采集并确认的终态允许后续操作回收占用；恢复入口及 job controls 可重新关联输出句柄，未知状态继续阻止重叠写入。文件内容哈希、补丁上下文检查和原子写入仍独立生效。

只读文件、目录、日志、任务查询及停止入口不申请写入占用。HTML 读取本地已有记录，不影响执行。

边界：外部终端、多控制端、跨 endpoint 的 AFS 别名、独立 worktree 共享的 Git common-dir 尚不协调。不透明 shell 如果 `cd` 到另一个目录或写入绝对路径，无法由选定 cwd 推知全部影响；调用方应选择覆盖预期写入范围的目录。direct绑定和显式cwd拒绝受管副本路径，远端监督器也检查解析后的物理cwd。固定入口只在共享其审计根的同类进程间协调，不声称不同状态后端之间有统一锁。普通锁不构成操作系统沙箱。

旧版 MCP 不理解本轮目录协调与副本退出规则，使用前应停止并重载所有共享该状态目录的 MCP 实例；不得混用旧进程。数据库不删除旧数据，但这不是旧程序可以接管新目标的保证。降级需要先停止相关控制端，并使用独立状态目录。

## 未知占用与人工接管

优先使用原 job 的 `resolve_operation`、`job_status` / `job_wait`、`stop_job` 恢复并核验终态。占用信息包含 claim、workspace、job 和输出标识。

若原任务仍无法核实，保留占用。人工通过原服务器的终端确认相关任务已停止后，可用本地 CLI 解除：

```sh
python -m remote_workbench.resource_claims --data-dir /absolute/local/state
python -m remote_workbench.resource_claims --data-dir /absolute/local/state --release CLAIM_ID --confirmed-stopped
```

此操作只移除本地协作占用，不发送信号、不删除远端内容，也不能证明进程树已清空。不要仅因 MCP 退出而解除未知占用。

## 修改记录

受管编辑记录本次补丁。`git_diff` 是显式获取当前 Git 差异；不存在源目录与副本之间的交付步骤。普通 shell 内部写入不自动生成逐文件补丁审计。

## 独立的执行位置

选定工作区是默认cwd，不规定工程布局。命令可显式选择其他相对或绝对cwd，并引用其他路径下的代码、解释器、环境脚本和结果。MCP不推断目录角色、不自动激活环境或复制依赖。代码/结果不必处在同一Git仓库；`git_status` / `git_diff` 只适用于 Git 工程；普通受管编辑在非 Git 目录中仍记录逐次 diff。快照编辑无需 Git；显式文本补丁需要 Git 程序但不要求目录为仓库。实际命令cwd与后台job origin保持明确。
