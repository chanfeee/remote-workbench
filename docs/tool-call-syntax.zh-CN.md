# 编辑快照与调用防错

2026-10-26 的完整任务对照没有复现 JavaScript／补丁格式语法错误，但仍发生旧上下文抄错和测试字符串过度转义。当前本地实现用显式文件快照减少这些输入；这不是所有模型生成错误已消除或额外 token 已低于 2% 的结论。

## 推荐流程

直接调用 `read_file(path="example.py", for_edit=true)`。返回有界的编号预览、`sha256`、实际 `path` 和 `edit_id`。只缓存这个文件，不复制工程、不扫描其他代码，主入口不创建本地暂存文件。

普通整文件修改直接传新内容的行数组：

```json
{"edits":[{"edit_id":"READ_RETURNED_ID","new":["def summarize():","    return 2"]}]}
```

小范围修改可以额外传 `start_line`、`end_line`，行号为原快照的 1-based、首尾包含。多个范围不能重叠，整文件替换不能与同一快照的范围混用。

这是 `apply_patch` 的参数。工具取得原快照、生成补丁，并在远端校验整份文件哈希；Agent 不必手抄旧内容、计算 hunk、拼接补丁标记或使用原生 `apply_patch` 暂存源码。行数组保留字面的反斜杠、引号和 Unicode；整文件替换沿用原文件的 LF/CRLF 与尾换行约定，空文件使用 LF 尾换行。工具不猜测 `\\n` 应不应该被解码，不修复业务代码。

高级兼容入口 `workspace_tools(tool="read_file", arguments={path:"example.py", for_edit:true, local_copy:true})` 仍可显式取得私有系统临时目录中的 `local_path`。修改此本地文件后用 `edits=[{edit_id}]` 应用。暂存修改尚未改变远端；主入口的无暂存快照必须传 `new` 或范围。

`check_only=true` 可显式预览；正常 apply 内部已经检查哈希和 Git 补丁，不必为了普通修改额外调用一次预检查。审阅变更后运行必要测试；没有新修改或具体疑点时不要重复已通过的完整测试或 CLI 子集。

## 减少模型往返的组合入口

显式绑定时可同时执行初始命令和读取明确列出的文件：

```json
{"host":"cluster-a","cwd":"/user/selected/repo","command":{"commands":[["git","status","--short"],["python3","-m","unittest","-v"]]},"read_files":["REQUIREMENTS.md"],"edit_files":["csvstats.py","test_csvstats.py"]}
```

这是 `workspace_switch` 的参数。`files` 按只读列表、编辑列表顺序返回；编辑项带 `edit_id`。最多十六个不同的明确路径，预览每文件受 `max_bytes`（默认4096）和160行限制，不自动发现/扫描其他源码，不创建暂存副本。初始命令未成功时，返回已成功的绑定与初始执行事实，跳过文件读取；个别读取失败则在该文件项记录错误。

修改后可显式指定后续命令：

```json
{"edits":[{"edit_id":"READ_RETURNED_ID","new":["new source line"]}],"command":{"commands":[["python3","-m","unittest","-v"],["git","diff","--check"],["git","diff"]]},"timeout":30,"max_bytes":8192}
```

这是 `apply_patch` 的参数。文件校验、补丁和后续命令沿用同一绑定与写入占用，避免中途切换目录或被其他受管写操作插入。命令必须由调用者明确传入；不识别“测试”后自动运行额外流程。`check_only` 不允许带命令，认证/计算审核守卫在修改前执行。补丁冲突或未成功时不执行命令。

补丁与命令是两个事实，**不是回滚事务**：`success=true` 表示修改已完成，命令事实在 `command_result`，非零、超时和 unknown 不会改写成修改未发生。后续命令无法确认时保留资源冲突；完整输出和父子关联保留在审计。取消请求也不能解释成修改已撤销。

成功应用后返回 `edits=[{path,edit_id}]`，来自刚应用的已知字节，减少为小修改重新读取的回合。这不是命令之后重新扫描的磁盘状态；命令、格式化器或外部程序如果又修改了文件，下次用此引用会触发原有哈希冲突。旧快照仍保留，不复用旧 ID 覆盖原内容。

组合是可选参数，原有单独调用继续可用，没有新增工具模式或通用编排 API。更新需要重载 MCP；已运行的旧进程不会自动获得新参数。

## 冲突、隔离与记录

- 主 `apply_patch` schema 只接受快照引用，不接受 `path/old/new` 或原始 `patch`，避免常用路径退回手抄上下文。公开工具仍为同一套八个，没有 full/compact 模式。
- 引用属于原聊天和 target。同一 MCP 重启后可继续使用仍存在的本地暂存文件；切换目标后必须切回或重新读取，不能拿旧引用写到新服务器。其他聊天不能读取或应用此引用。
- 文件在读取后被外部修改，即使修改处不在编辑范围内，也返回明确冲突。引用的原始快照或本地暂存文件损坏、替换为符号链接等，均拒绝；本地错误在写占用和 SSH 提交之前处理。
- 字节传输保留 CRLF、文件尾换行及反斜杠。远端先核对所有文件并检查 Git 补丁，再复用保留权限、所有权的原子单文件写入。
- 单文件写入是原子的；外部终端不受协作锁约束。多个文件不是跨进程事务：写入期间的外部修改或文件系统故障仍可能造成部分完成，需检查返回事实和操作记录，不能盲目重放。
- 引用绑定已选定的实际目录；`for_edit` 的 apply 路径需要 Git worktree。非 Git 文件仍使用已有、带 `expected_sha256` 的 `patch_file`。
- 完整传输与补丁保留在私有本地操作记录中，base64 仅是内部文件传输，不出现在正常工具回复中；模型只收到有界预览和引用。
- 暂存路径不是长期文档地址。系统清理临时文件后应重新读取；不会因此自动覆盖、迁移或删除远端文件。

## 兼容入口

旧 `edits=[{path,old,new}]`、原始文本／行数组、Add/Delete 及 unified Git patch 保留在 `workspace_tools(tool="apply_patch", arguments={...})`，固定工作区入口也保持兼容。调用旧精确上下文形式时仍拒绝不唯一或不匹配的内容，不进行模糊替换。

更新后需重载客户端 schema；旧进程不会自动获得新接口。`workspace_info.runtime` 显式提供版本、包路径和 data-dir，可区分同名 stable/develop 安装，不增加默认探测轮次。

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

## 验证层次

定向回归覆盖旧空行上下文错误、实际 Node 参数边界、快照范围和缓存文件、字节精确性、权限、外部冲突、批量预检查、跨聊天／目标拒绝、MCP 重启及旧入口兼容。自动化测试覆盖以对应验证说明为准；个人服务器验收过程和原始记录不进入仓库。

不追加日常模型调用、自动修复循环、科研验证计划、在线费用统计或新的功能配置模式。额外 token 是否低于 2% 仍需要后续新的完整任务对照，不能由参数字节变小推算。
