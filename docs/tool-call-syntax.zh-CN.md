# 命令、补丁与自动操作记录

本地开发候选优先沿用普通命令与本地同款补丁接口。命令、执行位置、输入输出和受管补丁由执行层记录；Agent 不需要为生成记录另开模型回合。以下接口能力尚不表示整体耗时或 token 已接近本地，实际客户端仍需单独验收。

## 首次命令可以同时选择目标

```json
{"host":"cluster-a","cwd":"/work/project","command":["python3","--version"]}
```

这是 `exec_command` 的参数。明确的 `host` 和绝对 `cwd` 在同一次调用内完成绑定与执行，不要求先调用 `workspace_switch`。以后省略 `host` 即使用当前聊天的绑定；相对 `cwd` 相对于该绑定解析。新聊天没有绑定且不提供目标时仍拒绝远程执行；连接失败不回退本地，不自动认证。

`workspace_switch` 仍可只绑定，也可组合明确的初始命令、`read_files` 和 `edit_files`。切换目标保持旧任务的原始位置与运行状态，聊天之间不自动共享绑定。`workspace_use_local` 返回本地模式。

命令可以是 shell 文本或字面 argv 数组。需要环境激活、管道或 here-document 时使用普通 shell 命令；需要多项顺序检查时可传 `command={commands:[[程序,参数],...]}`。环境不跨调用隐式保存。

```json
{"command":["python3","-c","import sys; print(sys.stdin.read())"],"stdin":"input text\n"}
```

文本 `stdin` 按 UTF-8 原样保存并传给命令，上限 1 MiB；命令批次的输入用于批次协议，不与 `stdin` 混用。认证及计算审核在绑定或执行前检查。普通命令默认返回至多 16 KiB 的预览，完整流保存在本地；只在确实需要剩余输出时用 `read_output` 分页。

## 重复传入同一目标

`exec_command` 再次给出已绑定的相同 host 和规范化实际 cwd 时，复用当前 target/revision，不再为这次命令额外探测、重新绑定目录。省略 host 的调用继续沿用当前绑定。

目标改变、尚未绑定或传入别名路径时仍重新核验。显式 `workspace_switch` 始终检查目录，可用于需要重新确认实际位置的情况。每条命令仍检查 SSH 连接并创建独立操作记录；绑定复用不保存 shell 激活状态，也不跳过目录协调、编辑冲突或计算审核。

工具公共说明只保留跨工具边界，具体参数由各工具 schema 和说明提供；不需要读取整份目录后才能使用命令／补丁。实际模型仍可能选择额外发现或审阅，接口不保证固定请求数。

## 命令结束后同时获取选定文件

```json
{"host":"cluster-a","cwd":"/work/project","command":["python3","-m","unittest","-v"],"read_files":["REQUIREMENTS.md"],"edit_files":["module.py","test_module.py"]}
```

主 `exec_command` 可完成目标选择、正常命令和显式文件读取，随后直接用返回的 `edit_id` 修改并检查，无需另开一次文件读取请求。也可继续使用普通命令、独立 `read_file` 和原生 patch；没有新增工具或执行模式。

`read_files` 和 `edit_files` 合计最多16个不同路径，只读取明确选择的文件。路径相对于命令实际启动的 cwd，绝对路径也可使用；不追踪任意 shell 内部 `cd`，环境激活不跨调用保存。`max_bytes` 用于命令预览，各文件预览使用同一预算且沿用64KiB上限；文件快照保留现有大小限制与冲突保护。

已确认完成且有明确退出码的命令可以继续读取，包括测试失败或 `git diff --no-index` 等非零退出；原退出码与失败输出保持不变，不能据此推断检查通过。超时、取消、未知状态或拒绝执行时返回 `files_skipped`。文件读取错误单独进入 `files`，不应因此重新运行已完成的命令。读取是命令之后的独立只读观测，不是跨外部程序的文件事务。每份文件记录关联原操作、原 target 和实际目录。

## 准确的工具标注

八个工具保持不变。`read_file`、`read_output` 和 `fetch_image` 通过 MCP annotations 声明不修改远端源码、聊天绑定或任务；本地审计、快照和预览缓存照常保存。它们限定在已绑定账户／工作区或本地已保存输出，不能据此执行任意 shell。

`exec_command`、`apply_patch`、绑定控制及混合 `workspace_tools` 网关没有被标成只读。特别是，网关的查询操作不意味着其中的写入／任务操作也能免审。标注不替代服务端认证、校验、目录协调或计算审核；调用方是否采用它及审批行为仍由客户端策略决定。[OpenAI 工具标注说明](https://developers.openai.com/plugins/build/mcp-server)

此优化不写入任何全局或项目审批策略，不自动开启 `approve`、不改沙箱或自动审批审核器。减少一次工具往返能减少一次潜在的审批／传递等待，但不是固定速度或2% token目标的保证。

## 使用本地同款补丁

```json
{"patch":["*** Begin Patch","*** Update File: run.sh","@@","-echo before","+echo after","*** Add File: test_run.py","+import unittest","*** End Patch"],"command":["python3","-m","unittest","-v"]}
```

这是主 `apply_patch` 的参数。接受 context patch 文本或逐行数组，也接受 unified Git diff；新增文件直接使用 Add File，不必先为不存在的文件申请快照。上下文、路径、已有目标冲突和受管写协调仍检查，不模糊修补错误内容。

`command` 是调用者明确给出的后续检查，只在修改成功后执行，沿用同一绑定与写占用。认证/计算审核在写入前完成。修改和检查分别记实：修改成功后检查非零或未知，不代表修改已撤销。输出未知时保留不确定性和资源冲突，不盲目重放。

`apply_patch` 同样接受 `read_files`／`edit_files`，在修改及后续命令结束后返回 `files`。例如格式化器改写源码后，可直接取得新内容和新快照，省去另一次读取。需要后续编辑时使用 `files` 内的新 `edit_id`；补丁阶段 `edits` 中的引用产生于后续命令之前。补丁冲突、执行超时／未知或被拒绝时不继续读取；已确认结束的非零检查仍保留文件和失败事实。未传文件选择时不会自动读取任何文件。

命令和后续检查的 `stdout/stderr` 保留本次预算内的原文，识别出的测试摘要只补充事实，不替换成功输出。只有截断、缺失或具体检查需要时才回读日志；普通测试完成无需为获取已产生的短报告另开一轮调用。

文本补丁使用已安装的 Git 程序做校验和应用，不要求目录是 Git 仓库，也不会初始化仓库。无 Git 程序时可使用下述原有快照编辑；快照写入不依赖 Git。

## 可选的完整文件快照保护

`read_file(path="example.py", for_edit=true)` 返回编号预览、`sha256`、实际路径与 `edit_id`；只缓存明确选定的文件，不复制工程或扫描其它代码。多个文件可用 `path=["module.py","test_module.py"]` 一次读取，最多16个不同路径，每文件分别受 `offset/limit/max_bytes` 限制并独立返回错误和引用。快照合计预算为配置单文件上限的两倍，超出项明确报错，不自动重试。

```json
{"edits":[{"edit_id":"READ_RETURNED_ID","new":["def calculate():","    return 2"]}]}
```

`patch` 和 `edits` 二选一。快照整文件替换使用已知新内容，Agent 不需要抄旧内容或计算 hunk；也可用1-based、首尾包含的 `start_line/end_line` 做范围替换。正常编辑不强制使用引用或行号。

引用绑定到原聊天、target和实际编辑根；外部更改即使在编辑范围外，也会触发整文件冲突。成功应用返回下一次 `edit_id`，已知内容的小修可直接复用；后续命令或格式化器再改文件时，旧引用仍会正确冲突。原始快照、权限和单文件原子写入检查保留，多文件不是跨进程回滚事务。

高级 `workspace_tools(tool="read_file", arguments={path:"example.py",for_edit:true,local_copy:true})` 可显式获取本地暂存文件再用 `edits=[{edit_id}]` 提交。主入口不创建暂存工程；临时文件消失时重新读取，不自动覆盖或迁移远端数据。旧 `{path,old,new}` 调用仍在高级入口保留。

## 用户能独立看到的记录

- 记录从工具执行时自动写入本地，包含用户命令、host、实际初始 cwd、提供的 stdin、stdout/stderr、退出状态、超时/取消及任务标识。SSH/shell传输包装保留在详情中。
- 每次受管编辑保存本次补丁、文件与 diff，HTML从本地数据自动刷新，关闭页面不影响执行。模型只获取本次有界结果和操作记录链接，不需要重新读取日志来替用户生成记录。
- 显式 `git diff` 是当前工作树差异；普通 shell 任意写入不等于完整逐文件审计。需要记录代码修改时使用受管补丁；对其它 shell 写入明确获取适用的差异，不宣称监控了任意程序的全部文件写入。
- 八个公开工具是一套接口，没有 full/compact 运行模式，也不强制固定三次调用。记录、token统计与工程任务规划相互独立；费用工具仅用于手动基准。

本地开发源码的接口变化需安装并重载客户端后才生效，运行中的 stable 不会自动更新。`workspace_info.runtime` 可显式查看实际版本、包路径与data-dir，不增加默认探测回合。

## 客户端与内容边界

命令优先使用 `exec_command(command=["python3","-m","unittest","-v"])`；确需 shell 管道或环境激活时才使用 shell 文本。同一目录的多项检查可用一次受监督的顺序批量执行：

```json
{"command":{"commands":[["git","status","--short"],["git","diff","--check"],["python3","-m","unittest","-v"]]},"timeout":30}
```

批量共用一次 SSH、一个新登录 shell 的环境、cwd、总超时及写入占用；每步是独立子进程，环境改动不跨步保存，stdin 关闭。默认首个非零停止；`stop_on_error=false` 继续，整体保留首个非零。返回每步的退出码与状态；中断时运行中的步骤为 `unknown`，未开始为 `not_run`。状态或身份不能确认时不推断成功。最多 32 步、1 MiB 参数，以 stdin 传送，stdout/stderr 流式写入同一操作记录。

命令别名 `argv`/`cmd`→`command`、`workdir`→`cwd` 只做明确映射；同时传别名与标准名会拒绝。其他拼写错误不猜测、不自动重试。缺少远端程序、正则错误、正常搜索无匹配、测试断言失败和 MCP 调用错误应分别判断。预期负例不代表产品故障，不把有父子关系的审计记录与 call_metrics 数量相加。

外层 `functions.exec` 在 MCP 收到请求前解析 JavaScript；服务端不能修复尚未送达的源码。可选的确定性调用生成器保持可用：

```sh
python -m remote_workbench.call_source docs/examples/edit-request.json
```

生成器拒绝重复键、非有限数字和无效 UTF-8，统一明确参数别名；固定 JavaScript 从 JSON 数据生成，并只输出 MCP 文本块，不重复序列化响应封套。它不执行请求或自动重试，也没有接管 App 的自由 JavaScript 调用桥。主入口限制和字节一致性测试不等于客户端绝不会犯语法或内容错误。

生成调用不依赖 `atob`、`TextEncoder` 等浏览器 API；外层执行环境未提供这些 API 时仍可使用。自由编写的脚本仍须遵守调用端提供的 API，MCP 无法捕获或修复尚未提交给它的源码错误。

成功前台命令回复始终保留实际 `remote_task.actual_cwd`；启动目录与它相同时可省去重复的 `launch_cwd`，空 `origin_nonce` 也可省略。不同目录、非空身份、清理限制及未知／失败／超时详情继续保留；原始操作记录和显式 `audit_get` 不做这项缩减。

## 验证层次

定向回归覆盖旧空行上下文错误、实际 Node 参数边界、快照范围和缓存文件、字节精确性、权限、外部冲突、批量预检查、跨聊天／目标拒绝、MCP 重启及旧入口兼容。自动化测试覆盖以对应验证说明为准；个人服务器验收过程和原始记录不进入仓库。

不追加日常模型调用、自动修复循环、科研验证计划、在线费用统计或新的功能配置模式。额外 token 是否低于 2% 仍需要后续新的完整任务对照，不能由参数字节变小推算。
