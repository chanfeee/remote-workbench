# 开发专用分层计时

`devtools/mcp_timing.py` 只供源码工作区排查执行延迟。正式入口不导入它，没有环境变量或 MCP 参数可开启它；日常回复、HTML 和操作记录结构不增加计时数据。wheel 和 sdist 明确排除 `devtools/` 及其专用测试，独立 stable 安装不变。

## 显式启动

在开发工作区使用开发解释器启动，输出目录必须是全新的本地目录：

```sh
.venv/bin/python devtools/mcp_timing.py serve \
  --output .local/dev-timing/run-001 -- \
  --config /path/to/hosts.yaml \
  --data-dir .local/dev-timing/run-001/state \
  --viewer-port 0
```

这是开发专用 MCP stdio 进程。其标准输出仅承载原有 MCP 协议；用开发客户端或临时的工程级配置显式连接该进程。不要更改全局 stable 注册或日常 launcher。关闭后正常入口的行为不变，不需要在正式产品中保留调试开关。

计时器不会建立或重新认证 SSH。仍使用原配置和已有人工认证的 ControlMaster；工具目标、环境、审核、锁、超时、取消和任务监督规则继续由原实现执行。启动器只包装固定的**本地函数边界**，不包装进程创建 API、不使用 preload、不向远端注入计时代码。

目录中的 `events.jsonl` 持续保存原始计时事件，正常退出后生成 `summary.json`。可在进程运行期间或异常退出后独立查看已有完整事件：

```sh
.venv/bin/python devtools/mcp_timing.py summarize .local/dev-timing/run-001
```

同一目录不会被再次用于新进程，不覆盖旧报告。目录权限为 0700，数据文件为 0600。磁盘写入失败只停用本次诊断并向 stderr 提示，不改变原操作的返回值或异常；原始命令及输出仍由正式审计路径保存。

## 测量边界

| 层 | 记录内容 |
|---|---|
| `decoded_receive` → `reply_queued` | 本机 MCP 已解码请求到回复进入发送队列 |
| `mcp.dispatch` | SessionMCP 的参数规范化、SDK 调用和结果转换 |
| `sdk.validate_and_invoke` / `session.handler` | SDK 校验与会话处理、路由、执行和回复打包 |
| `session.identity` / `registry.*` / `route.*` | 本地身份解析、状态读写和目标选择 |
| `workspace.lease_acquire` / `lease_release` | 会话锁的获取和释放，不把整个操作持锁时间当成锁等待 |
| `resource.acquire` / `finish` | 受管目录写入占用检查与完成处理 |
| `operation.execute` | 原工具处理器的完整执行 |
| `ssh.control_check` / `prepare` / `transport` | ControlMaster 检查、执行协议准备、SSH 调用及输出采集 |
| `audit.*` / `reply.projection` | 固定本地审计函数调用与回复摘要处理 |

每个调用以随机关联 ID 对应其嵌套 span，并通过原有随机审计 ID 关联命令和修改记录，无需把诊断字段塞进 MCP 回复。记录不包含工具参数、命令、输入输出、主机、目录、聊天 ID、密码或异常正文；只保留层名称、随机关联／审计 ID、时间和异常类型。后台采集器不在请求结束后继续产生日常计时。

**不同层的 inclusive 耗时相互包含，不能相加当作总耗时。** 多个并行请求也可能时间重叠。总量优先读取 `server_receive_to_reply_queued_ms`；`receive_to_dispatch_ms` 与 `dispatch_to_reply_queued_ms` 展示目前内层计时之外的 SDK 间隙。关联缺失、取消或连接关闭时保留 incomplete，不伪造完成时间。

协议起点在 stdin 解码之后，终点在实际 stdout flush 之前。它不直接测量 App 排队、客户端传输、模型时间、远端 CPU 或远端进程初始化内部阶段。SSH transport 时间也不能解释成 Python CPU 时间。若与 App 外层日志配对，跨进程／客户端的差额只能报告为尚未测量的边界，不能直接归到某种语言。

诊断本身的本地写入也有开销，且可能包含在外层 span 内。它用于定位阶段；最终性能对照应关闭诊断，不能把启用计时后的数字直接当作正常运行速度。

## 验证范围

专用测试覆盖接口和返回值不变、错误及取消传播、并发隔离、锁语义、退出后恢复原函数、诊断写入失败、请求结束后的后台行为、真实本地 stdio 成功／失败关联，以及本地 worker 模拟 SSH 的命令、补丁、日志和监督。模拟 SSH、真实 SSH 和 App 实际加载分别报告。

2026-10-29 的补充验收已核实实际 App 加载开发启动器、真实 SSH 小工程的命令／修改／测试，以及计时与原审计 ID 的对应。调用端 await 与服务端区间使用独立记录对照；跨进程校时只给出位置范围，不把缺少内部观测的等待全部归到审批、App 或 Python。一次审批超时未进入服务端，按工具允许重试一次后成功。完整边界见仓库 `VALIDATION.md`，原始主机、目录、调用与计时结果仅保存在私有开发报告中。这不是新增的完整 Agent token 对照。

诊断不会调用模型或记录 token/credits，不作为普通任务的完成条件。原始测速数据和个人连接配置只保存在被忽略的 `.local/`，不进入 Git 或发行包。后续性能修复仍在普通代码中独立验收，不依赖此工具长期在线。
