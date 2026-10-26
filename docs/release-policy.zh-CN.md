# 分支与发行规则

2026-10-26 起采用两条长期分支。开发/稳定是源码和安装渠道，不是 MCP 的功能模式；两者使用同一套接口设计。

| 名称 | 用途 | 更新方式 |
|---|---|---|
| `main` | 开发主线，接收已完成的功能与修复 | PR 以 main 为目标，CI 通过后维护者手动合并 |
| `stable` | 验收后的发布候选源码 | 从 main 选定候选提交，创建指向 stable 的推广 PR；CI 与所需验收通过后手动合并 |
| `vX.Y.Z` / GitHub Release | 固定、可回退的发行版本 | 对选定 stable 提交创建版本 tag 和 Release；不移动已发行 tag |
| `develop` | 迁移期保留的旧开发分支 | 已有代码与未提交工作保留，后续通过 PR 迁入 main；不再作为新的长期开发目标 |

不自动合并 PR，不在每次 main/stable push 时自动发布，不因 main 更新而更换日常安装。紧急稳定修复应同步回 main，避免两个渠道长期分叉。

## CI 和手动合并

CI 对 main/stable 的 push，以及目标为 main/stable 的 PR 运行 Python 3.10—3.13 测试和构建。维护者检查这些结果后手动合并。建议在 GitHub Settings → Rules / Branches 为两分支要求 PR 与以下状态检查：

- `Tests (Python 3.10)`、`Tests (Python 3.11)`、`Tests (Python 3.12)`、`Tests (Python 3.13)`。
- `Build package`。

这份 workflow **不等于** GitHub 分支保护。当前连接对 branch protection API 返回 `403 Resource not accessible by integration`，因此本轮未宣称已配置服务端合并门禁；需由有权限的维护者在 Settings 设置。保持自动合并关闭。

## 准备一次正式 Release

1. 在 main 准备最终版本号 `X.Y.Z`，同步 `pyproject.toml` 和 `uv.lock`，通过 PR/CI。
2. 将选定代码推广到 stable，完成所需本地、真实服务器和 App 加载验收。模拟 SSH 不能替代实机验收。
3. 在 Actions 手动运行 **Prepare stable release**，运行分支选 main，输入 stable 已包含的版本号。该 workflow 必须先合入默认分支 main，GitHub 才会提供手动运行入口。
4. Workflow 从 stable 取固定提交，拒绝脏源码、版本不一致、开发/预发布版本，以及指向其他提交的同名 tag；重新测试并构建 wheel/sdist、依赖约束和 SHA256SUMS。
5. 成功后只创建 **未发布的 draft Release**，目标是已验证的完整 SHA。维护者核对发行说明和附件后再手动发布。重复运行遇到已有 release 不会静默覆盖资产；应先检查现有 draft。
6. 日常环境从该 release 的固定 wheel 安装到新目录，验收后再切换启动器；保留旧目录以便回退。不使用 editable 安装，不自动跟随 stable 分支移动。

最初 stable 从既有日常安装对应的 `1ea7dbabebc8bff224835757c5e970f37759cf43` 建立，其版本为历史 `0.3.0a10`。这只是保留既有稳定基线，**尚不是新的正式 Release**。首次正式发版需先确定最终版本号；本轮不替用户选择版本或发布现有 alpha。

参考：[GitHub 手动运行 workflow](https://docs.github.com/en/actions/how-tos/manage-workflow-runs/manually-run-a-workflow)、[gh release create 的 draft/target 行为](https://cli.github.com/manual/gh_release_create)。
