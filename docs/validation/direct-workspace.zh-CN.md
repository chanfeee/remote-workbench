# Direct工作目录的自动化验证

本文件记录目录模式的可移植验证范围。个人服务器、来源目录、节点、会话标识及真实SSH验收流程不进入仓库。

## 本地模拟覆盖

- `test_direct_workspace.py`：默认direct在用户选择的原目录执行；不存在工程副本模式；旧文件和历史不删除。
- `test_session_server.py`：会话绑定独立，来源与实际cwd明确；切换目标不改变旧任务origin；旧副本绑定和任务拒绝执行/恢复。
- `test_mcp_job_recovery_stdio.py`：使用本地SSH替身与真实MCP stdio进程，注入启动回复丢失、重启和目标切换，恢复原任务和输出游标，验证没有重复启动。
- 目录协调测试覆盖同一目录、父子重叠、不同目录、只读访问、未知任务占用，以及采集确认终态后的解除。

```sh
python -m pytest -q tests/test_direct_workspace.py tests/test_session_server.py tests/test_mcp_job_recovery_stdio.py
```

## 边界

运行状态与direct代码目录分离；不创建隐式项目副本，也不自动删除或迁移旧copy内容。

目录协调限于共享本地状态、同一SSH endpoint下已知重叠的受管目录，不限制外部终端、多控制端、跨endpoint文件系统别名、共享Git common-dir或shell超出声明目录的写入。进程组清理不等于已确认完整进程树清空。

本地模拟不证明任何真实服务器或客户端已加载当前代码。使用方应独立验证其认证、shell、文件系统和客户端环境；相关操作原件仅保存在本地运行记录中。
