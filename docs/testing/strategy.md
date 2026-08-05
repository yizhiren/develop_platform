# 测试策略

- 单元测试：状态机、RBAC、Schema、错误分类、命令推断和脱敏。
- 集成测试：SQLite、Outbox、租约、Redis Streams、API 和 Artifact Store。
- Provider 契约：GitHub/GitLab 的分支、PR/MR、CI、Webhook 和合并。
- Agent 测试：Fake Provider、协议 Stub、工具权限和结构化产物。
- 代码闭环测试：临时裸 Git 仓库、工作区 clone、真实文件修改、pytest、commit/push、PR Stub 与 Diff 证据。
- 安全测试：路径逃逸、资源限制、提示注入、Secret 泄漏、越权，以及生产 Compose 中 Sandbox Executor 实际断网。
- 端到端测试：单仓、跨仓、Review 返工、验收重设计和逐仓合并。

DeepSeek 真实测试只有 `RUN_LIVE_AI_TESTS=1` 时运行，模型为 `deepseek-v4-flash`，并发 1，每套最多 20 个请求。除了五类结构化协议，还包含一个完整本地 Git 交付用例：模型必须修复故障代码并跑通 pytest，Git Worker 随后提交和推送工作分支。默认 CI 不消耗付费额度。

恢复验收必须证明：清空 Redis 后可由 SQLite 恢复任务；Worker 强制退出后任务可重投；SQLite 备份可在干净环境恢复并通过 checksum、完整性和迁移版本检查。当前已自动验证 Redis/Outbox 重发和 SQLite 干净恢复，并通过真实子进程读取任务后 `SIGKILL`、Pending=1、替代消费者 `XAUTOCLAIM`、ACK 后 Pending=0 的演练。

容量验收使用真实 Redis 租约演练：在 `MAX_PARALLEL_REQUIREMENTS=1` 时第二个需求不能获取槽位，第一个释放后第二个可获取。非实时模型后端套件当前为 36 个测试；DeepSeek live tests 默认单独运行，避免普通 CI 消耗额度。
