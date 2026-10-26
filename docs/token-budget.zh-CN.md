# 独立完整任务 Token 基准（develop 原型）

本文件仅说明手动运行的独立、可重复基准样本，不是日常开发流程或普通任务完成门槛。该基准的固定比较阈值为 **2%**，并要求保留原有功能、编辑保护和审阅能力：

`(远端任务 total_tokens − 本地任务 total_tokens) / 本地任务 total_tokens ≤ 2%`

Remote Workbench 自身的日常命令、编辑和测试路径不采集 token/credits 或模型费用，不为计量增加模型调用；日常工作流不在回复或 UI 中追加用量评估。此约定仅限 Remote Workbench 自身的执行与集成路径，不声明 Codex App 或模型服务如何记录账户用量。基准结论不会阻止普通任务完成。离线费用账本与 `scripts/check_task_budget.py` 保留为基准测试辅助工具；完整操作记录与必要执行诊断照常保留。

这里的 total 包含全部输入（含缓存）和输出（含推理）；缓存输入、推理输出均是子项，不能重复相加。计数来自执行 agent 的实际 usage。回复字节压缩率、静态 tokenizer 估算、缓存命中率和账户整数百分比均不能替代这一验收。

**当前这份基准样本未通过比较阈值。** 最新两名独立 Agent 均为 9 次请求，本地 355,677、远端 372,086 tokens，额外 4.613%。两侧同模型/推理设置、相同冻结代码与需求，21 项独立验收均通过，全部错误修正计入。此前报告中的 1% 是当时的目标，保留历史原件；当前基准阈值为 2%。不回算或剔除失败回合来宣称达标。

## 精简模式

开发版 session 启动器增加 `--tool-profile compact`，不改变默认的 full 模式，也不修改独立 stable 安装。常规可见接口有八个：

- `workspace_switch`：指定本聊天明确需要的 host/source cwd，可同时传 `command` 做首次检查，不需要先查身份/连接再绑定。
- `exec_command`：读取、搜索、Git 和测试；独立检查可以合并为一次命令。环境在同一命令内显式激活。
- `apply_patch`：接受 `*** Begin Patch` 上下文补丁或既有 unified Git diff。
- `read_output`：按需补读完整输出，不自动补读已成功的测试清单。
- `fetch_image`：拉取聊天内图片。
- `workspace_info`、`workspace_use_local`：本地元数据和取消远端选择。
- `workspace_tools`：保留后台任务、原目标停止、交付、记录和恢复等全部高级功能，按名称调用。

例如 `workspace_tools(tool="describe", arguments={"name":"start_job"})` 只返回所需工具的 schema；随后用 `workspace_tools(tool="start_job", arguments={...})` 调用。网关沿用原工具的参数校验、threadId、锁、计算审核和作业原目标路由，不能传入另一个聊天身份。

每个聊天首次结果、绑定切换、恢复和错误会提供操作记录链接。后续成功回复复用该工作区链接，完整记录自动归档，不需要 Agent 逐条收集链接。MCP 进程重启后首次回复会给出新链接。

精简模式仅对可以完整识别的 unittest 成功输出返回测试数和状态；完整 stdout/stderr 留在本地记录中。部分页、失败、警告、未知输出和执行状态不明时保留原有诊断。退出码、超时、清理不确定性、未知扩展字段和输出读取句柄不会被误报为成功。

上下文补丁在远端编译为 unified diff，一次 `git apply --check` 成功后再应用。检查失败不会先写入已处理文件；不匹配或多义上下文、越界路径和符号链接会拒绝。原始提交保存在 stdin 审计中，HTML 显示编译后的标准 diff，模型不会再收到已提交补丁的全文回显。编译器不运行额外 shell，也不使用 preload/spawn 拦截。

## 基准样本的有效性与阈值判定

仅在手动启动独立基准时，采用两名全新独立执行 agent：同一模型/推理设置、同一冻结代码和需求、相同独立验收。控制器、准备、报告和任何诊断试跑另列，不能把它们藏进一侧或删掉执行 agent 的重试。若要求每侧九次请求，超过九次则这份基准样本不合格；这里的“不合格”只表示样本未满足比较条件，不影响普通任务是否完成。

模型的代码和推理输出本身会变化。单次通过只证明该次样本；不能用长上下文填充分母，也不能用简单任务的一次负增量保证所有工程任务都不超过 2%。保留逐请求 usage、工具发现、检查、读取、修改、测试、审阅和最终回复的完整账本。完整任务差额也不能全部归因于纯 MCP 协议：独立生成的代码、测试和脚本会不同。

仓库提供不调用模型的门槛脚本：

```sh
python3 scripts/check_task_budget.py --native native.json --remote remote.json --requests 9 --threshold 2
```

每份 JSON 格式：

```json
{"model":"实际模型","reasoning_effort":"实际设置","baseline_sha256":"0000000000000000000000000000000000000000000000000000000000000000","task_sha256":"1111111111111111111111111111111111111111111111111111111111111111","requests":9,"acceptance_passed":true,"complete":true,"usage":{"input_tokens":10000,"cached_input_tokens":8000,"output_tokens":1000,"total_tokens":11000}}
```

将示例的哈希替换为实际冻结基线和需求的 SHA256。仅当两侧哈希、模型和推理设置相同、全部有效性条件成立且不超过阈值时退出 0；刚好 2% 可通过，超过 2% 不合格；不合格退出 1；无效数据退出 2。`complete`、验收、哈希和模型设置需要外部日志/测试证据，脚本不会凭这些字段独立证明它们。它不计算套餐扣减，也不把 Standard credits 折算当作实际额度。

本机仓库的 `.codex/config.toml` 可用同名 `remote-workbench` 项目覆盖测试该候选，指向 `.local/develop/bin/remote-workbench-session` 并传 `--tool-profile compact`。这不会新增第二个 MCP，也不改变其他项目使用的全局 stable；App 仍可能缓存旧工具，需要在当前项目重载。通用安装继续默认 full，独立 stable 不随源码编辑而更新。

OpenAI 官方文档建议将跨工具约束写入 MCP `instructions`，并保持前 512 字符可独立理解：[MCP 文档](https://learn.chatgpt.com/docs/extend/mcp)。订阅内用量不能仅由 credits 单价推出：[用量与价格](https://learn.chatgpt.com/docs/pricing)。
