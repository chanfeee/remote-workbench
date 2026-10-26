# 结构化传输的自动化验证

本文件只记录可移植的工程测试覆盖，不包含个人服务器、目录、聊天或操作记录。

## 覆盖范围

- `test_tool_call_syntax.py`：在实际Node.js执行边界核对生成的固定调用桥、JSON字符串、引号、反斜杠、Unicode及调用组合；拒绝重复键、非有限数字和无效UTF-8。
- `test_prepared_edits.py`：读取快照、新内容行数组、范围、CRLF和尾换行、文件权限、外部修改冲突、跨聊天/目标拒绝、重启及兼容入口。
- `test_structured_calls.py`：字面argv、参数别名、冲突和非法输入在远程提交前拒绝，认证和计算审核保持有效。
- `test_batch_runner.py`、`test_command_batch.py`：顺序argv批量、逐步状态、首个非零停止或继续、超时、取消及不完整进度的unknown语义。
- `test_task_lifecycle.py`：使用有界本地进程验证任务监督、截止时间和取消清理。

结构化传输集成阶段的完整本地回归为674项通过；随后组合接口阶段的验证见[组合接口验证](combined-workflow-20261026.zh-CN.md)。这些都是本地模拟与工程回归，不是个人集群实机日志。

```sh
python -m pytest -q tests/test_tool_call_syntax.py tests/test_prepared_edits.py tests/test_structured_calls.py tests/test_batch_runner.py tests/test_command_batch.py
```

## 保证边界

固定序列化与哈希/补丁检查不解释或自动修复源码字符串。测试只证明覆盖的输入；MCP不能保证客户端在请求发送前生成的任意JavaScript或业务代码永不出错。

保留原有冲突保护、超时、取消、任务origin和完整本地审计；原始输出按需回读。批量命令不是事务或shell沙箱，进程组清理也不是完整进程树的强隔离。工程测试、响应字节数和接口调用次数不能替代完整任务的token验收。
