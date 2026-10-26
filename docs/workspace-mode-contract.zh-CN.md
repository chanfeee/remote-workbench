# 工作区 direct/copy 契约（A1/A2 设计交付）

状态：设计契约，direct 尚未实现。原始草案基线：`develop` / `bebcbea1427c39f46f6f2fcb579a33f72c6b2797`（`0.3.0a11`）；job origin 的 C2 现状已按 `ae380a9`（a13）核对。本文件覆盖后续 A1/A2；当前阶段不改变 copy 默认值，也不把 A3 的 direct 默认值提前。

## 1. 当前数据与行为证据

| 概念 | 当前表示与行为 | 当前缺口 |
|---|---|---|
| 聊天 / workspace | `workspaces` 以 `workspace_id` 为主键、`thread_id` 唯一；一个线程有一个当前 host/source/cwd/project/target/revision。 | workspace 是聊天状态容器，不是物理目录身份；没有 `binding_mode`。 |
| 来源目录 / 仓库 | `workspace_switch(host, cwd)` 先远端 `realpath`，记录所选 `source_cwd`；Git 项目另由准备脚本找 `repository_root`。 | target 保存 `source_cwd`，不单独保存规范 `source_root`、存储命名空间或资源 ID。Git remote URL 只被复制，不适合作资源主键。 |
| 实际执行目录 | `targets.cwd` 是实际工作 cwd，`targets.project_root` 是项目根。`SessionRouter.switch()` 总是为 target 生成 `.remote-workbench-sessions/.../<target_id>/project` 并调用 `prepare_project()`，所以当前为每聊天私有 copy。 | API 无 direct 选项。copy 的实际 cwd 和原始 source cwd 是不同目录。 |
| target / 历史 | `targets` 当前唯一键为 `(workspace_id, host, source_cwd)`，target 行被视为不可变；`target_history` 按 workspace revision 记录切换。 | 同一聊天同 host/source 不能同时有 copy 与 direct target；表没有显式 schema/格式版本。 |
| job 起源 | `jobs` 和 `job_operations` 保存 `target_id`、host、`source_cwd`、cwd、`project_root`，以及 C2 持久化的 `origin_nonce`、`launch_cwd`、`actual_cwd`、`remote_state_dir`、`ssh_target`；幂等键和 `job_id` 受 workspace 约束。切换目标不会改已登记 job 的目标。 | 还没有 `binding_mode`、资源身份或独立的 `runtime_state_ref`；具体状态路径已由 `remote_state_dir` 保存。扩展时应在保留该现有路径的基础上补齐新身份字段，不能按当前 active target 重算。缺少完整原始身份的旧行仍保持 unknown。 |
| 运行状态 | 本地 registry/audit 位于 `REMOTE_WORKBENCH_DATA_DIR` 或 XDG 数据目录；executor 当前把 remote state dir 放在 `project_root/.remote-workbench-state`。copy 的 `session.json` / `baseline.json` 位于远端会话副本旁。 | 代码/工作目录与运行状态路径耦合。direct 若沿用当前规则，会把工具状态写进用户源项目；必须先解耦。 |
| 写入协调 | `SessionRouter.lease()` 对本地 state dir 中的 `locks/<workspace_id>.lock` 用 `flock`，只按聊天串行。remote worker 以 executor 的 remote state root 放 writer lock；job collector 的 `.lock` 只防止重复采集同一日志。 | 不同聊天可同时写相同目录；worker lock 不涵盖任意 `exec_command` 或远端后台任务，也没有 worktree 与 Git common-dir 两级资源键。 |

代码核对点：`workspace_registry.py` 的 `Registry.__init__/commit_switch/register_job/reserve_job_operation`；`workspace_router.py` 的 `switch/lease/executor`；`session_server.py` 的 `switch`、工具 lease 分类与 job origin；`session_project.py` 的私有快照和基线准备；`remote_worker.py` 的 `state_root/lock`；`job_collector.py` 的本地句柄键和采集锁；`config.py` 的 `HostConfig`。`workspace_session.py` 也只创建一次性 copy，不提供 direct 契约。

## 2. 身份字段与显式 API

字段必须分别表达以下身份，不能互相代用：

| 字段 | 含义 | 不能据此推断 |
|---|---|---|
| `thread_id` | MCP 客户端提供的聊天身份。 | 目录或共享范围。 |
| `workspace_id` | 本地 registry 中该聊天的稳定状态/审计分区。 | 实际目录唯一性。 |
| `source_root`, `source_cwd` | 来源项目根，以及用户选择的来源 cwd（可为仓库子目录）。 | 执行 cwd。 |
| `cwd`, `project_root` | 该 binding 的实际执行 cwd 与项目根。copy 指向私有副本，direct 指向所选源树。 | 资源锁 ID。 |
| `binding_mode` | `copy` 或 `direct`，不可由路径猜测。 | 是否经过 OS 隔离。 |
| `resource_id` | 实际执行树的稳定资源身份；copy 是副本资源，direct 是用户源树资源。需携带 `identity_scope`，以界定此 ID 的协调范围。 | target_id、未映射的其他 host alias、仓库 URL。 |
| `identity_scope` | 第一版在同一 configured SSH endpoint 内使用 endpoint-scoped 身份（稳定 endpoint ID 可由配置项标识默认派生）；跨 endpoint/alias 共用身份需另行显式映射。 | 不表示不同控制端之间有分布式锁。 |
| `git_common_resource_id` | Git worktree 共享的 common-dir 元数据资源身份；非 Git 项目为 null。 | worktree 文件树身份。 |
| `delivery_resource_id` / `delivery_git_common_resource_id` | 仅 copy 使用：`workspace_apply` 要写回的来源树/来源 Git 元数据身份。 | copy 执行树身份。 |
| `runtime_state_ref` | 后续独立的远端作业、日志、worker 锁等运行状态命名空间引用；当前 C2 job origin 已持久化具体路径 `remote_state_dir`，但尚无独立引用字段。 | 代码目录或 source root。 |
| `target_id` | 一条不可变的 workspace-to-target 绑定记录。 | 跨聊天共享资源身份。 |

建议 target v2 记录至少包含：`binding_mode`、`target_format_version`、`identity_scope`、上述适用的资源 ID、`runtime_state_ref`、当前既有的 `target_id/workspace_id/host/source_cwd/cwd/project_root`。job 与 idempotency operation 也要快照 `binding_mode/identity_scope/resource_id/git_common_resource_id/runtime_state_ref` 及执行目录，作为不可变 origin，同时保留现有 C2 字段和已保存的 `remote_state_dir`。历史行记录 mode 和 resource id，便于复原当时绑定；切换 active binding 不更新历史 job。

工具契约：

```text
workspace_switch(host, cwd, binding_mode="copy", share_resource=false, expected_revision?, ...)
workspace_info / workspace_resume -> 同时返回 binding_mode、target_id、cwd、project_root、resource identity、runtime_state_ref
```

- `binding_mode` 只接受 `copy|direct`；省略时保留旧语义 `copy`。本批仅定义显式 `direct` 可用时的行为，不切换默认值。
- `copy` 延续私有快照；重复选择同一个 `(workspace_id, host, canonical source_cwd, copy)` 恢复原 target，不覆盖副本，不因丢失而隐式重建。
- `direct` 在远端验证并规范化用户选中的实际目录后，`cwd` 是来源树内相同相对位置，`project_root` 是该实际仓库/目录根；不创建或克隆副本。若目录或所需状态根无法验证，拒绝绑定。
- 第一版针对普通单一 configured SSH endpoint，可用 `identity_scope=endpoint:<stable configured endpoint ID>` 加 endpoint 内规范路径生成 `resource_id`。协调范围限同一台本地控制端、同一 control state dir 和已知相同或重叠的目标目录；普通 direct 不要求额外配置跨集群 namespace。跨 endpoint/alias 的共享资源识别是后续可选增强。
- 建议资源键为 `SHA256("rwb-resource-v1" || NUL || identity_scope || NUL || scope-relative canonical path)`；endpoint scope 下 canonical path 由该 endpoint 的远端路径验证得到，跨 endpoint scope 则必须先使用已配置 namespace 映射。copy 的执行资源可由不可变 `target_id` 派生，copy delivery 资源则按来源 endpoint/path 另算。
- 当已有另一个 workspace 绑定同一 `resource_id` 时，新的 direct 请求须显式 `share_resource=true`。同一 target 的恢复不重复询问。`share_resource` 是用户允许共享实际目录的记录，不代表它能识别未映射的其他 endpoint/host alias。
- 第一版不承诺跨 endpoint/alias 共享协调。只有显式请求跨 endpoint/alias 共享时，才要求 namespace/path-prefix 映射证明它们归一到同一资源 ID；没有映射时返回 `resource_identity_unknown`，不授予并发写 claim。普通单 endpoint 的 direct 无需这类配置；已知目标目录相同或重叠时仍须在本地控制端内协调。
- 同一 workspace 可保留同一 host/source 的 copy 与 direct 两条 target；切换 mode 是带 `expected_revision` 的 CAS，失败重读 `workspace_info` 后再决定，不改 target_id、不挪动文件。target 表的唯一键至少扩展为 `(workspace_id, host, source_cwd, binding_mode)`；如需同 mode 多个副本槽位，再加显式 `target_slot`，不得用随机路径制造不可恢复的重复 target。
- 为兼容已发布调用，现有 `workspace_switch` 省略 mode 永远按 v1 copy 解释。A3 验收后再发布显式 v2 入口（例如 `workspace_switch_v2`，其新建默认 direct、仍可显式 copy）；不让旧客户端省略参数后意外绑定到 direct。

## 3. Registry 格式迁移、幂等与回退

当前数据库只 `CREATE TABLE IF NOT EXISTS`，没有 registry 格式版本。迁移应在独立实现任务中进行：

1. 加入 `registry_meta.schema_version`；数据库打开时只做有版本号、可重复执行的迁移。迁移前用 SQLite backup API 留存可校验备份，停止并发 schema writer；在一个事务内迁移并验证行数、主外键、唯一性和 job origin，然后提交版本号。
2. 所有既有 target、history、job、job_operation 无条件标记为 `binding_mode=copy` / `target_format_version=1`。沿用原 `target_id`、`source_cwd/cwd/project_root`、revision、job_id、idempotency_key、fingerprint 和状态；保留 job 已保存的 `remote_state_dir` 及 C2 origin 字段；不重建、不重新 clone、不清理远端文件。旧 copy 的执行资源 ID 可派生自 target_id；其 delivery/source 资源按当前 endpoint scope 解析，若之后需跨 alias 合并则必须先有 namespace 映射。新 `runtime_state_ref` 应指向行内已保存的 `remote_state_dir`；缺少该路径或其他必要身份字段的旧 job 保持 unknown，不按当前 active target 补猜。
3. 因为 SQLite 不能直接移除 `targets` 的旧唯一约束，要用事务性表重建来增加 mode 维度；复制所有原列和行，再校验历史唯一 revision 及 jobs 后替换。新写入的 copy/direct 分别按新唯一键做幂等查找。`workspace_id` 仍是 thread 唯一分区，不作为资源唯一键。
4. 同一请求重放：相同 workspace、host、canonical source、mode、slot 返回既有 target；相同 idempotency key 仍须匹配原命令 fingerprint 与完整 target origin，否则拒绝。资源 claim/lease 用 CAS token 防止两个进程都获得同一资源；仅超时不能自动释放一个已启动 job 的 claim。
5. 事务/校验失败时整体回滚，保留备份并让服务 fail closed；迁移后保留只读备份。旧二进制无法理解 direct origin，因此当新版本已创建 direct binding/job 时，不允许把数据库降级后交给旧二进制读写。迁移及 direct 写入期间必须避免旧进程并发写同一状态；本文不声明新旧版本混写安全。回退运行旧代码前只能恢复到“无 direct 写入”的兼容备份；若 direct 之后产生了数据，继续用支持 schema v2 的构建并关闭 direct，或先导出 direct 记录和运行状态供新版本恢复，不删除实际目录、不把 direct 伪装成 copy。

## 4. Copy 基线/交付与代码/状态解耦

| 操作 | copy | direct |
|---|---|---|
| `workspace_changes/preview/apply` | 现有语义继续有效：相对副本初始内容 baseline 比较；preview 给每文件 source/session 状态守卫；apply 写回来源并使用来源资源锁、Git common-dir 锁及 hash/version 冲突检查。现有实现是逐文件应用，不承诺多文件事务。 | 返回结构化 `not_applicable`（mode、reason、operation）；不得把源树误认成副本或执行“写回自己”的 apply。direct 用户的改动已在实际目录发生。 |
| 初始 copy baseline | 远端 `baseline.json` 是来源/副本初始状态，用于 copy 的差异和交付；保留 manifest version 与 session marker。 | 不创建这种源到副本 baseline。若将来需要“绑定时快照”，应另建只读 observation 类型并与 delivery 分开，不冒用 F1/F2。 |
| 外部修改 | 写回 source 前逐文件核对 preview 时的 source state；变化则 conflict，不覆盖。 | 所有 Remote Workbench 受管写接口用资源锁加 expected hash/CAS；检测不符即 conflict。未持锁的外部编辑无法被 advisory lock 阻止。 |

拆分三类存储：本地 control state（registry/audit）、远端 runtime state（job、stdout/stderr、worker bookkeeping、采集进度）、用户代码树（copy 或 direct）。copy 旧 target 的 runtime 路径保持原值；direct 新 target 的 runtime root 必须是用户可写但在代码树之外的远端私有状态目录，并按 `runtime_state_ref` 稳定复用。不得把 runtime 文件放进 direct Git worktree。对新旧远端状态目录的权限、容量、清理和跨进程共享行为由 F4 另验；资源身份和运行状态引用都加入不可变 job origin。

## 5. A2 资源协调边界

### 第一版：同一控制端

- 保留 workspace binding 的 `revision` CAS，但把写入 lease 从 `workspace_id` 扩展为资源 lease。第一版只协调同一台本地控制端、同一 `state_dir` 且已知相同或重叠目标目录的多个 MCP 进程/聊天；它们必须看到同一 registry、lock directory 与 lease 表。lock 文件以规范化资源 ID 的 hash 命名，不能以聊天 ID 命名。
- 写实际 worktree 时取得 `resource_id` 锁；会触碰 Git common metadata 的工具取得 `git_common_resource_id` 锁；copy apply 还取得 delivery/source 两个 ID。多个锁按排序后的 resource ID 顺序申请以避免死锁。copy 项目工作树与来源树是不同资源。
- `exec_command` 的 shell 不透明，按“可能写”处理，持有当前 worktree 和已知 common-dir lease 直到前台命令结束；不靠命令名白名单推断只读。后台作业在远端确认终态前持续占用对应资源；状态 unknown 时不自动解锁。锁只约束同一状态目录下经过 Remote Workbench 的调用，不会阻止外部终端、用户编辑器或其他程序，也不是操作系统隔离。
- `start_job` 不只在启动 RPC 期间持锁：成功/状态未知时登记 durable resource claim，把 job origin 与目录/common-dir resource IDs 绑定。job 在远端确认终态前，其写资源 claim 阻止新的写调用并返回明确 `resource_busy/job_unknown`；日志断连、MCP 重启或本地句柄缺失不能自动清 claim。`job_status/stop_job/job_wait` 及本地 info/history/audit/control 不取得写资源锁；它们可继续查看或请求停止。stop 未确认时 claim 保留为 unknown。
- 操作锁采用短轮询/异步等待或快速 busy 返回，不能在 event loop 上阻塞。每次取得锁生成单调 fencing token 并写入 lease 记录；Remote Workbench 自有 worker 写接口在提交时校验 token 和 expected hash。进程退出导致 OS flock 释放时，过期 lease 标记为 `unknown` 并 fail closed，需对 job/操作做恢复核验后明确清除；不得根据 heartbeat 超时就假设远端命令已停。查询、日志和 stop 不持有写 lease，仍可使用。
- 对脱离 SSH 前台进程的后台子进程、改写任意路径的任意 shell，当前架构无法全面约束。A2 只能对记录中的工作树及其 common-dir、且经过 Remote Workbench 协调的入口提供并发串行化；不宣称命令级沙箱、任意进程锁或 exactly-once。

### Git 与工作树

资源解析为两个不同键：`worktree_resource_id` 标识实际目录树；`git_common_resource_id` 标识 `git rev-parse --git-common-dir` 得到的 Git 元数据目录。Git worktree 文件树不同、common-dir 相同的两个 binding：允许文件级 worktree 写并行；涉及 common-dir 的受管写串行。常规 `git` 命令/所有 `exec_command` 可能修改 index、refs、config 或 lock files，故至少锁目标 worktree 与已知 common-dir。目标之外的任意仓库或 `cd` 后路径不在可证明范围内。

### Host 别名与 AFS（后续可选增强）

`host alias + realpath`、`dev/inode`、仓库 remote URL 都不能证明两个 SSH host 的路径是同一 AFS/共享资源。第一版仅承诺 endpoint-scoped 身份在同一控制端和同一状态目录内的协调；普通 direct 用户无需配置完整 storage namespace。跨 server AFS 自动归一、多个控制端共用锁及分布式锁都是后续可选增强。只有需要跨 endpoint/alias 共享时，才配置稳定 `storage_namespace_id` 和 path-prefix，将 `(host alias, remote prefix)` 映射成 `(namespace_id, namespace-relative canonical path)`；未映射时返回 `resource_identity_unknown`，不授予共享写 claim。不同控制端、不同 state_dir、锁语义不可靠的 AFS/NFS/共享盘不在第一版保证范围；锁也不阻止外部终端或构成 OS sandbox。实际挂载映射、锁语义与故障恢复需后续隔离环境实测。

## 6. 可执行验收矩阵

以下是 A1/A2 实现后必须转成自动化测试；涉及真实 AFS/SSH 的项目另加隔离实机步骤。表中“拒绝”应返回稳定错误码，并保留可诊断 target/resource/job 标识。

| 场景 | 操作 | 通过条件 |
|---|---|---|
| 不同仓库 | 同一聊天依次绑定 repo A、repo B 的 copy/direct。 | 目标的 source root、实际 cwd、resource IDs 分别正确；切换不改另一 target 与其文件。 |
| 同仓独立目录 | 两聊天选择同仓库 copy；另用两个不同 worktree/direct 路径。 | copy 实际路径互异；不同 worktree 的 worktree IDs 不同；若 common-dir 相同，metadata ID 相同。 |
| 同聊天同来源两种 mode | 对相同 host/source 先 copy 再 direct，再切回 copy。 | 两个 target 可共存；direct cwd 指回源树；copy cwd/文件与原 target 保持；revision/history 正确。 |
| 显式共享实际目录 | 同一 endpoint、同一控制端和状态目录下，两聊天对相同目标路径发 direct；第二次显式 `share_resource=true`。 | 两 target 有不同 target_id、相同 resource_id；写操作串行；未显式共享时拒绝。 |
| host 别名（后续可选增强） | 以两个 endpoint/alias 选同路径，测试显式 `share_resource`、namespace 映射与缺映射。 | 仅在实现跨 endpoint/alias 共享时验收；缺映射时返回 unknown，不授予共享写 claim。 |
| 同仓不同 clone | 两个 clone remote URL 相同但目录不同。 | resource IDs 由 identity scope/path 得到不同值，不以 URL 错误合并。 |
| Git worktree 元数据 | 两工作树共享 common-dir，同时一个改工作文件、一个做受管 metadata 写。 | 文件树可独立；metadata 写互斥；锁键和错误里能区分两类资源。 |
| 模式省略与旧 API | 旧请求不带 mode；新请求显式 direct；后续 v2 入口省略 mode。 | v1/旧行均解析为 copy；direct 仅显式启用；A3 v2 默认行为与旧 API 分离。 |
| 重启、切换与旧 job | copy 中 start_job，重启 MCP、切到 direct/另 repo，再用 job_id 查询/停止。 | job 始终路由原 host/cwd/project/runtime_state/mode；active target 变化不重写 origin。 |
| 旧 copy 数据 | 从 v1 数据库迁移；检查副本、marker/baseline、运行状态、历史、job/idempotency。 | 原路径和 IDs 不变；副本内容、日志与 job 起源可读；重复迁移幂等；无隐式重建/删除。 |
| direct 不适用 delivery | direct 调用 changes/preview/apply。 | 返回 `not_applicable`，不创建 copy baseline、不向 source 执行自我交付。 |
| 外部修改 | 取得预览或读 hash 后，在协调工具外改文件，再 apply/direct guarded write。 | copy apply/direct write 都报冲突且不覆盖外部数据；不持锁修改明确不在互斥保证内。 |
| 锁竞争与控制不阻塞 | 聊天 A 持长前台写锁或存在 active job，聊天 B 试写并同时调用 info/status/stop。 | B 写为可取消等待或快速 busy；info/status/stop 不等待写锁；停止未确认时 durable claim 仍在。 |
| 锁中断/失效 | 杀掉持锁 MCP；或令 job collector/SSH channel 断开；第二进程请求写。 | flock 可由 OS 释放，但 durable lease/job claim 转 unknown 并阻止写；不能超时自动抢锁；完成恢复核验后才解锁。 |
| 并行 MCP | 两个进程使用同一个 control state dir、不同聊天同时绑定同 direct resource。 | SQLite CAS 与资源 lease 只授予一个写者；不同 resource 可并行；进程重启后可恢复状态。 |
| 状态/代码分离 | direct 中检查 runtime state 路径、重启恢复 job；copy 中升级工具。 | direct runtime state 不在用户代码树；旧 copy 继续读原路径；容量或权限不足显式失败。 |

## 7. 依赖、边界与未验证项

- A1 实现依赖：registry schema 迁移、target/snapshot/API 新字段、`workspace_switch` v1 copy 兼容与显式 direct、resume/info/history/job origin 序列化；远端目录解析需区分来源路径、Git 根、实际 cwd 和 runtime state root。
- A2 第一版依赖：同一 control-state directory 内的 resource-ID resolver、worktree/common-dir 双资源协调、durable background-job claims、fencing/CAS、写冲突保护、控制工具绕开写 lease。跨 endpoint 的 storage namespace 配置属于可选增强。单给 `patch_file` 加锁不满足 A2。
- Registry schema 迁移由 A1 独立设计并与 session 序列化集成。a13 的 D2 测试证据失效实现属于历史行为，不是 A1/A2 当前依赖；不要求共用数据库迁移版本。其他紧邻依赖：C2/C3 决定 job origin 恢复及未知态何时可清 claim；F1/F2 决定 copy baseline 与 delivery guard 的字段兼容；F4 决定远端状态目录的配额和保留策略。不得由 A1/A2 文档宣称这些已完成。
- 未实测：本契约本轮只读本地源码，未 SSH、未连接真实 host/AFS。需验证同一 state dir 下多 MCP 进程的 `flock`/SQLite 行为；endpoint-scoped 路径解析；远端 worktree/common-dir lock；MCP 被杀后 foreground shell/job 的真实存活与状态恢复；direct runtime state 的权限、容量和路径策略。跨 endpoint AFS namespace 映射是可选增强，不是普通 direct 的前置配置。
- 结论边界：当前实现仍是私有 copy；没有 direct mode、跨聊天资源锁或 durable resource claim。本文是后续 A1/A2 的设计契约，不是实现或实机验收记录；A3 direct 默认值仍须等显式 direct 验收后作为独立变更切换。
