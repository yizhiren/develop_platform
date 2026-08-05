# 里程碑 3：升级、恢复与干净验收

- 日期：2026-08-05
- 状态：本地完成，外部 Provider 验证待配置

## 交付内容

- SQLite `schema_migrations` 与 v1/v2/v3 事务迁移；运行中的旧 Volume 已自动增加 `pull_request_url` 和稳定 `agent_key` 字段。
- 工作区租约、72 小时默认 TTL、10 GiB 默认总配额及 Compose 清理工具。
- Review 拒绝回开发、验收拒绝回架构修订再开发的证据回传测试。
- 两仓按顺序人工触发合并、每仓 Checks/head SHA 重校验、合并后最终验收测试。
- 普通验收 fresh checkout 已发布工作分支，最终验收 fresh checkout 合并后目标分支，并固定 SHA。
- Redis 丢失时从 SQLite Outbox 重发，技术错误用新任务 ID 指数退避，避免完成锁吞掉重试。
- 控制台可选择需求涉及的仓库和合并顺序，显示工作分支、PR/MR、head SHA、交付状态，以及每次 Agent 运行的稳定身份/阶段/模型/Token/错误。
- GitHub Checks 同时覆盖 Check Runs 和传统 Commit Status；GitHub/GitLab HTTPS 用户名分别修正为 `x-access-token` 与 `oauth2`。

## 验证

| 验证 | 结果 |
| --- | --- |
| 后端非联网套件 | 31 passed，6 live tests 默认排除 |
| 前端 lint | 通过 |
| 前端 production build + SSR | 2 passed |
| Compose | API、Web、Redis、Agent Worker、Git Worker 健康 |
| 数据库升级 | 实际 Volume 包含迁移 v1/v2 和新增列 |
| API 冒烟 | 四 Agent Fake 链路到 `awaiting_merge`，五类产物齐全 |
| DeepSeek 代码闭环 | 真实修复、pytest、commit/push、PR Stub 通过 |
| 备份恢复演练 | 在线备份、SHA-256、干净临时恢复、`integrity_check`、迁移 v1/v2/v3 校验通过 |
| Agent Secret 隔离 | Worker `/proc/1/environ` 无模型 Key，启动临时文件已删除，子进程最小环境 |

## 发现并修复的问题

- Git Worker 在纯 merge 任务上过早初始化 `/workspaces`，导致无写权限测试回归；改为分支内惰性初始化。
- 最终验收结果原先忽略 `approved=false` 并错误完成需求；已改为拒绝进入 `BLOCKED` 并增加测试。
- 技术重试复用任务 ID 会被 Redis 完成锁吞掉；改为新任务 ID 与独立 AgentRun。
- UI 原先默认把项目全部仓库绑定到需求，无法表达子集与顺序；已改为显式选择。
- 仓库列表原先把尚未验证的连接显示为“已连接”；已改为“待验证/已验证”。
- Agent 可通过测试代码污染开发目录 `.git`；发布改为从可信 URL 重新 clone 元数据，只同步无 symlink 的普通工作树后再注入凭据。
- Entrypoint 单纯 `unset` 后密钥仍出现在主进程环境检查中；改为 `env -u` 创建干净 exec 环境，并验证临时文件已删除。

## 外部阻塞

本地环境未配置 `GITHUB_TOKEN` 或 `GITLAB_TOKEN`，因此没有权限对真实外部仓库执行 push、创建 PR/MR 和 merge。Provider HTTP 合约、本地 Git 数据面和 DeepSeek 编码闭环已验证；完成真实 Provider 验收需要按 Provider 接入文档配置一个最小权限测试仓 Token。
