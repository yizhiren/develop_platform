# 里程碑 1：控制平面与四 Agent 可运行纵切

- 日期：2026-08-05
- 状态：完成

## 交付内容

- 单组织认证、用户管理、项目成员 RBAC、项目/仓库/需求 API。
- 持久化需求状态机、乐观版本控制、三轮业务失败阻塞、暂停/恢复/取消。
- 澄清、架构、开发报告、Code Review、验收五类版本化产物；后续 Agent 会收到此前已确认产物与目标仓库上下文。
- SQLite Outbox、Redis Streams、Agent/Git Worker 分流、任务租约去重、过期 Pending 自动认领和结果游标。
- GitHub/GitLab Provider 合约、Webhook 验签与投递去重、合并前 CI/head SHA 重校验、逐仓合并状态。
- Web 工作台的项目、仓库、需求发布、需求详情、产物、时间线和人工闸门。
- SQLite 在线备份与完整性校验；非 root 容器和 Agent 文件/命令沙箱接口。

## 验证证据

| 验证 | 结果 |
| --- | --- |
| Python 单元/契约/安全测试 | 16 passed，5 live tests 默认排除 |
| DeepSeek 真实协议测试 | 5 passed（clarify/architect/develop/review/accept） |
| Web lint | 0 errors / 0 warnings |
| Web production build + rendered HTML | 2 passed |
| Compose 健康 | Web、API、Redis、Agent Worker、Git Worker 均运行 |
| Fake Provider 端到端 | 5 类产物齐全，停在 awaiting_merge 人工闸门 |

## DeepSeek 兼容性记录

模型列表确认 `deepseek-v4-flash` 可用。首次请求在默认思考模式下出现超时/空 `content`；根据官方 V4 接口约定，对确定性结构化 Agent 显式设置 `thinking.type=disabled`，随后五种协议全部通过。Provider 对偶发空内容只重试一次，Schema 不合法只修复一次，避免无上限计费。

## 当前边界

- 开发 Agent 已有结构化协议和受限工作区执行器，但自动 clone、编辑、提交、推送 PR 的完整工具循环尚未接入；当前 `development_report` 代表交付协议，不应被误认为已经写入真实仓库。
- GitHub/GitLab 真实合并需要用户配置 Provider Token、PR/MR number 与 head SHA；本里程碑用 MockTransport 验证 Provider 合约，未对用户真实仓库做写操作。
- SQLite 目前以 `create_all` 初始化新库；正式升级策略需在下一里程碑引入版本化迁移。

## 下一里程碑

实现需求级代码工作区：可信 Git Worker 准备/发布仓库，开发 Agent 在隔离容器内执行读写与测试工具循环，Review/验收使用实际 Diff 和测试证据，并完成一个受控测试仓库的真实 PR 闭环。
