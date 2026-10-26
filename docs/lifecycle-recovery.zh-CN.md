# 后台任务恢复（develop）

本文适用于 `remote-workbench-session` 入口。后台任务启动成功但回复丢失后，恢复操作会查找原任务，不重新执行原命令。切换当前聊天的服务器或目录，不会改变已保存 job 的原始位置。

## 使用现有入口

1. 启动后台任务时提供稳定的 `idempotency_key`。如果启动回复丢失，保留这个键，不换键重试。
2. 调用 `resolve_operation(idempotency_key=...)`。它可能通过现有 SSH 连接只读核验原任务，接回本地输出句柄和采集器；返回原 `job_id`、target 和 `local_job`。
3. 用原 `job_id` 调用 `job_wait` 或 `job_status`，后续传回 `next_stdout_cursor`、`next_stderr_cursor`，按字节继续读取。`stop_job` 也按保存的原目标控制任务。

compact 模式通过 `workspace_tools` 调用这些现有工具。没有新增恢复 MCP 接口。

阶段1中，MCP 负责记录明确目标上的命令、文件和后台任务生命周期事实；科研解释、验证是否充分以及被操作项目的测试适用性由调用 Agent 和用户判断。

`workspace_resume(include_legacy_test_history=False)` 默认只返回 binding、jobs 和 note 操作事实，不读取或修改测试缓存。设为 `true` 时只读返回 `legacy_test_history`；其中历史记录对当前代码的适用性仍为未知，不触发 SSH 扫描。恢复状态不证明远端当前仍在运行。明确恢复后，采集器自行续读日志，`job_wait` 等待本地进展，不反复查询远端状态。

`workspace_snapshot(scan_content=False, max_files=10000, max_content_bytes=67108864)` 默认执行显式元数据与 Git 检查；只有 `scan_content=true` 才扫描绑定的 `project_root`，可能包括选定 cwd 之外的仓库子目录。扫描沿用现有排除规则、不跟随符号链接，并受文件/目录条目数和文件内容字节预算约束；不完整时返回原因而非有效完整指纹。快照不会自动维护项目验证结论。compact 仍保留有界摘要；对 unittest/subTest，顶层测试数与报告的跳过数可能不属于同一计数层级，因此无法确定的执行数和通过数继续保持未知。

## 恢复能够确认什么

- 提交前保存原工作区/target、host 别名与 SSH endpoint、启动目录、状态目录和任务关联标识。别名与实际 SSH 连接目标分别处理。
- 请求的启动路径与核验得到的实际目录分别保存；符号链接路径不会被直接拿来与物理路径作错误比较。
- 核验任务 ID、状态位置和节点/启动身份；即使已有退出码，也核对持久 origin/status。已结束任务不要求原 PID 仍存活。
- 同一任务重复或并发恢复只发布一个本地输出句柄，并通过本地进程锁保持一个有效采集器。采集按已有 stdout/stderr 字节数继续，不重复拼接。
- 未通过身份核验的实际目录不会写回 registry。日志被截断、路径/身份不符或文件不完整时保留原本地输出，不能据此宣称成功。

这些协调发生在同一台本地控制端；这里不声明跨控制端分布式锁或文件系统沙箱。

## 未知状态与旧记录

连接不可用、原 host 不再配置、endpoint/节点/任务身份不匹配，或记录不完整时，返回 `execution_state=unknown` 和具体原因。恢复不会通过再次执行命令消除不确定性；连接失效时仍由用户恢复 SSH 认证。

数据库只增加可空的任务关联字段，不改绑旧 target。已有完整旧本地句柄可据其保存身份继续核验和控制；缺少完整身份的旧记录保持未知，不根据当前默认目录补猜 origin。旧进程不会自动获得新保护，客户端实际加载版本需要另行确认。

## 验证范围

C2 使用本地 worker、模拟 SSH 和临时独立工程验收，包括真实 MCP stdio 子进程重启、启动成功回复丢失、切换目标、分页续读、并发句柄和身份拒绝。测试不连接真实服务器，不代表 Codex App 已加载此代码。

## 测试历史与结果边界

a13 曾实现 D3 后台测试尝试、终态采集和 `last_test`/适用性关联；当时的 538 项集成通过是历史验收记录，见[开发任务与执行状态](development-plan.zh-CN.md#a13-历史提交执行记录2026-10-26)。这些实现和结果不构成当前默认管理职责。当前默认路径不登记测试尝试、不推断或维护 `last_test`/当前代码适用性，也不由后台采集器补写测试证据。显式读取旧历史时，`legacy_test_history` 保持只读，并将当前适用性视为未知。

作业终态和测试解释是不同事实：当前作业字段 `execution_state`、`exit_code` 和 `timed_out` 等记录执行结果；compact summary 使用 `process_status`。有界测试摘要只保留能从输出明确得到的框架信息。unittest 的 `tests_run` 是顶层数量，`reported_skips` 可能包括 subTest，不能相减得到执行数；有跳过或计数层级不明时，`executed`、`passed` 保持未知。不能仅凭退出码 0 宣称测试充分或适用于当前项目。测试命令的选择、结果解释和验证充分性由调用 Agent 与用户负责。

## a13 历史实现与验收

历史提交 `ae380a9`（a13）曾包含后台测试尝试登记、终态证据采集、`last_test` 与当前代码关联。其本地模拟演示覆盖启动回复丢失、MCP 进程重启、切换目标后接回原 job、日志续读，以及较早后台任务完成不覆盖较新前台观测；当时的集成记录为 538 项通过，详见[开发任务与执行状态](development-plan.zh-CN.md#a13-历史提交执行记录2026-10-26)。这些行为和结果仅是历史事实，不描述当前测试文件或默认管理职责。

## 当前默认范围复现

```sh
.venv/bin/python -m pytest -q tests/test_mcp_job_recovery_stdio.py tests/test_default_execution_scope.py
```

当前复现范围是后台 job 的恢复、日志续读和原 target 路由，以及默认路径不自动登记测试尝试、不读取或更新测试缓存、不采集测试证据。结果由本轮验证记录说明。
