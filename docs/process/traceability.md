# 需求追踪矩阵

| 需求 | 设计证据 | 计划实现 | 验证 |
| --- | --- | --- | --- |
| 多项目多仓库 | 产品需求、数据模型 | 已实现 API 与 UI | API smoke 通过 |
| 四 Agent 流程 | 状态机、Agent Runtime | 已实现 Worker、产物 Schema 与开发工具循环 | Fake E2E、DeepSeek 协议及真实代码闭环通过 |
| Review 返工 | 状态机 | 已实现 Review transition | 三轮阻塞单元测试通过 |
| 验收重设计 | 状态机 | 已实现 Revision transition 与测试强制复跑 | 完整拒绝→重设计→重开发链路测试通过 |
| 三轮阻塞 | 状态机 | 已实现 retry counters | 单元测试通过 |
| 跨仓顺序合并 | 总体架构 | Git Worker/MergeAttempt 与逐仓组合回归已实现 | 双仓本地 Git 编排通过；真实外部仓待测 |
| SQLite 轻运维 | ADR-0002 | Control Plane/backup 已实现 | 在线备份与干净恢复、checksum、完整性、v1/v2/v3 迁移校验通过 |
| Redis 可重建 | ADR-0003 | Outbox/reconciler、新 ID 技术重试和租约接管已实现 | 丢失重发、退避/耗尽；真实 Worker SIGKILL 后 `XAUTOCLAIM` 接管并 ACK 通过 |
| Secret 与执行隔离 | 威胁模型、ADR-0004、0008 | Worker/Sandbox boundary 与断网执行器已实现 | 路径/命令安全；PID1 无 Key；测试子进程实际联网失败 |
| DeepSeek 真实测试 | ADR-0005 | OpenAI-compatible Provider 已实现 | 5 类协议 + 真实代码/Git 交付 live_ai 通过 |
| 开发 Agent 真改代码 | ADR-0004、0006 | WorkspaceSandbox、DeveloperToolLoop、GitWorkspaceManager | 本地裸仓 clone/test/commit/push/PR Stub 通过 |
| 干净验收 | Agent Runtime、ADR-0006 | 工作分支/合并目标分支 fresh checkout + SHA 固定 + 测试复跑 | 单仓与双仓编排测试通过 |
| 可升级 SQLite | ADR-0007 | `schema_migrations` 有序事务迁移 | v1→v2 运行中 Volume 升级及幂等测试通过 |
| 方案基于真实代码 | Agent Runtime、ADR-0006 | 方案前可信只读 clone 与限额仓库分析 | 仓库内容提取及调度顺序测试通过 |
| 每仓合并后回归 | 状态机、Agent Runtime | 增量组合 checkout + Agent4 回归门禁 | 双仓真实本地 Git 前后组合与失败阻塞测试通过 |
| 全局并发上限 | 总体架构、ADR-0009 | Redis 需求租约、心跳与 TTL 释放 | 真实 Redis 上限 1 阻塞/释放/接管演练通过 |
| 大型证据不可变保存 | 数据模型、ADR-0009 | SHA-256 内容寻址 Artifact Store + RBAC 下载 | 摘要去重、路径逃逸、Diff 外置测试通过 |
| 四 Agent 独立模型配置 | Agent Runtime、ADR-0010 | 稳定身份配置解析、共享回退与独立 Key 文件 | 单元测试、四角色 DeepSeek 实时请求、PID 1/临时文件检查通过 |
| 主机可见仓库与 SSH Push 容器 | 总体架构、ADR-0011 | Git Worker 独占 Git 网络操作；主机工作区绑定挂载；`.ssh` 只读单向挂载 | Compose 路径展开、SSH URL 校验、容器读写/凭据边界与真实 GitHub SSH 验证 |
