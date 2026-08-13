# 测试策略

- 单元测试：状态机、RBAC、Schema、错误分类、命令推断和脱敏。
- 集成测试：SQLite、Outbox、租约、Redis Streams、API 和 Artifact Store。
- 运行态测试：Worker 取得租约后通过结果流上报 `running`，Control Plane 在不推进需求状态机的前提下更新任务与 AgentRun，并在终态清理租约字段；过期运行任务可在重启恢复时重新排队。
- Provider 契约：GitHub/GitLab 的分支、PR/MR、CI、Webhook 和合并。
- Agent 测试：Fake Provider、协议 Stub、开发与验收工具权限、逐项证据覆盖和结构化产物。
- 代码闭环测试：临时裸 Git 仓库、工作区 clone、真实文件修改、pytest、仅本地 commit、独立 Review、批准后 push、PR Stub 与 Diff 证据。
- 安全测试：路径逃逸、资源限制、提示注入、Secret 泄漏、越权，以及生产 Compose 中 Sandbox Executor 实际断网。
- 依赖准备测试：锁文件识别、冻结安装参数、生命周期脚本默认关闭、缓存隔离、网络错误重试、路径逃逸和“依赖成功后才启动 Agent3”的工作流门禁。
- 需求关闭测试：关闭原因必填、排队任务与 AgentRun 终止、未发布 Outbox 丢弃、迟到 Worker 结果无副作用，以及远端合并执行期间拒绝关闭。
- 端到端测试：单仓、跨仓、Review 返工、验收重设计和逐仓合并。

DeepSeek 真实测试只有 `RUN_LIVE_AI_TESTS=1` 时运行，默认模型为 `deepseek-v4-flash`，并发 1，每套最多 20 个请求。除了五类结构化协议，还逐一验证 Agent1 至 Agent4 的独立配置能够请求模型，并包含一个完整本地 Git 交付用例：模型必须修复故障代码并跑通 pytest，Git Worker 随后创建本地 commit；系统架构师收到同一分支、SHA 与 Diff，批准后 Git Worker 才推送工作分支。默认 CI 不消耗付费额度。

恢复验收必须证明：清空 Redis 后可由 SQLite 恢复任务；Worker 强制退出后任务可重投；SQLite 备份可在干净环境恢复并通过 checksum、完整性和迁移版本检查。当前已自动验证 Redis/Outbox 重发和 SQLite 干净恢复，并通过真实子进程读取任务后 `SIGKILL`、Pending=1、替代消费者 `XAUTOCLAIM`、ACK 后 Pending=0 的演练。

容量验收使用真实 Redis 租约演练：在 `MAX_PARALLEL_REQUIREMENTS=1` 时第二个需求不能获取槽位，第一个释放后第二个可获取。非实时模型后端套件当前为 103 个通过；DeepSeek live tests 默认单独运行，避免普通 CI 消耗额度。

开发 Agent 还必须覆盖两个相反场景：脚本化 Provider 耗尽步骤时，错误详情需包含实际改动数、成功验证数、最近动作和 token；真实 DeepSeek 在隔离文档仓库中应能读取目标、修改文件、运行允许的聚焦验证并在预算内 `finish`。运行数据目录 `data/` 必须从平台自身 TypeScript/ESLint 扫描中排除，避免关联仓库污染平台检查。

验收 Agent 必须覆盖：从确认规格获得全部验收项和验证方法；在角色对应的 SHA 校验 checkout 中读取文件并运行独立命令；每个通过项只接受直接关联的平台证据 ID；遗漏、重复、伪造证据、复跑失败、缺少独立测试及验证命令改动初始文件均不得批准。Fake Provider 也必须走相同 `AcceptanceAction` 协议，确保默认测试不会绕过真实工具边界；显式开启实时测试时，DeepSeek 还需在隔离计算器仓库中自行选择并跑通 pytest 后才能批准。
