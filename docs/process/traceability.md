# 需求追踪矩阵

| 需求 | 设计证据 | 计划实现 | 验证 |
| --- | --- | --- | --- |
| 多项目多仓库 | 产品需求、数据模型 | 已实现 API 与 UI | API smoke 通过 |
| 四 Agent 流程 | 状态机、Agent Runtime | 已实现 Worker、产物 Schema 与开发工具循环 | Fake E2E、DeepSeek 协议及真实代码闭环通过 |
| Review 返工 | 状态机 | 已实现 Review transition | 三轮阻塞单元测试通过 |
| 验收重设计 | 状态机 | 已实现 Revision transition 与测试强制复跑 | 完整拒绝→重设计→重开发链路测试通过 |
| 三轮阻塞 | 状态机 | 已实现 retry counters | 单元测试通过 |
| 跨仓顺序合并 | 总体架构 | Git Worker/MergeAttempt 与逐仓组合回归已实现 | 双仓本地 Git 编排通过；真实 GitHub PR #4 合并与最终验收通过 |
| SQLite 轻运维 | ADR-0002 | Control Plane/backup 已实现 | 在线备份与干净恢复、checksum、完整性、v1/v2/v3 迁移校验通过 |
| Redis 可重建 | ADR-0003 | Outbox/reconciler、新 ID 技术重试和租约接管已实现 | 丢失重发、退避/耗尽；真实 Worker SIGKILL 后 `XAUTOCLAIM` 接管并 ACK 通过 |
| Secret 与执行隔离 | 威胁模型、ADR-0004、0008 | Worker/Sandbox boundary 与断网执行器已实现 | 路径/命令安全；PID1 无 Key；测试子进程实际联网失败 |
| DeepSeek 真实测试 | ADR-0005 | OpenAI-compatible Provider 已实现 | 5 类协议 + 真实代码/Git 交付 live_ai 通过 |
| 开发 Agent 真改代码 | ADR-0004、0006 | WorkspaceSandbox、DeveloperToolLoop、GitWorkspaceManager | 本地裸仓 clone/test/commit/push/PR Stub 通过 |
| 干净验收 | Agent Runtime、ADR-0006、里程碑 18 | 工作分支/合并目标分支 fresh checkout + SHA 固定 + 开发测试复跑 + Agent4 只读独立测试 + Control Plane 证据门禁 | 单仓与双仓编排；逐项覆盖、伪造证据、漏验和工作区修改测试通过 |
| 可升级 SQLite | ADR-0007 | `schema_migrations` 有序事务迁移 | v1→v2 运行中 Volume 升级及幂等测试通过 |
| 方案基于真实代码 | Agent Runtime、ADR-0006 | 方案前可信只读 clone 与限额仓库分析 | 仓库内容提取及调度顺序测试通过 |
| 每仓合并后回归 | 状态机、Agent Runtime | 增量组合 checkout + Agent4 回归门禁 | 双仓真实本地 Git 前后组合与失败阻塞测试通过 |
| 全局并发上限 | 总体架构、ADR-0009 | Redis 需求租约、心跳与 TTL 释放 | 真实 Redis 上限 1 阻塞/释放/接管演练通过 |
| 大型证据不可变保存 | 数据模型、ADR-0009 | SHA-256 内容寻址 Artifact Store + RBAC 下载 | 摘要去重、路径逃逸、Diff 外置测试通过 |
| 四 Agent 独立模型配置 | Agent Runtime、ADR-0010 | 稳定身份配置解析、共享回退与独立 Key 文件 | 单元测试、四角色 DeepSeek 实时请求、PID 1/临时文件检查通过 |
| 主机可见仓库与 SSH Push 容器 | 总体架构、ADR-0011 | Git Worker 独占 Git 网络操作；主机工作区绑定挂载；`.ssh` 只读单向挂载 | Compose 路径展开、SSH URL 校验、容器读写/凭据边界与真实 GitHub SSH 验证 |
| 需求澄清对话 | 产品需求、状态机 | 结构化问题清单、持久化用户回复、Agent1 上下文回传与规格版本更新 | 工作流上下文单测、前端构建与页面文案测试 |
| 方案评审与开工授权 | 产品需求、状态机 | 结构化方案评审区、批准/退回门禁、调整意见持久化并回传系统架构师 | 工作流反馈上下文单测、前端构建与页面文案测试 |
| 开发提交与 Review 双向交接 | 状态机、Agent Runtime、ADR-0006 | 开发完成后由可信 Git Worker 创建仅本地 commit；分支/SHA/Diff 传给系统架构师；驳回 findings 原样回传开发工程师；批准后才 push | 本地裸仓验证 commit 前远端无分支、Review/Push SHA 一致；工作流上下文单测 |
| 旧需求重试兼容 | 状态机、里程碑 10 | 缺失工作区时先重建；已有工作区时复用；重试重置对应失败预算；自动化开发禁止无仓库降级 | 3 个重试路由回归测试、3 个 Worker 边界测试、完整后端套件 |
| Agent 失败可诊断 | Agent Runtime、里程碑 11 | 独立步骤耗尽错误码；脱敏详情与 token 持久化；阻塞诊断、恢复建议和时间线原因展示 | 错误链路/脱敏/迁移/UI 文案测试；真实 DeepSeek 开发循环通过 |
| 开发与评审证据收敛 | Agent Runtime、里程碑 12 | 探索硬上限、大文件保护、断言式验证；旧 Review 只反馈给开发、不进入下一轮 Review | 真实仓库三轮隔离回放；真实需求本地 commit；评审上下文回归测试 |
| SSH-only 人工 PR 闸门 | Git Worker、里程碑 13 | Compare 链接、Owner 登记、reviewed SHA/URL 校验、登记前隐藏合并；Provider Token 缺失时前后端共同禁止自动合并 | URL 与接口单测；前端类型/构建/渲染测试；真实管理员负向 API 回归；真实需求保持待合并 |
| 待合并需求自动创建 PR | Git Worker、里程碑 14、16 | 非敏感能力标志；Owner 请求经 Outbox 交给 Git Worker；创建/复用 PR 后校验 reviewed SHA 并回写；手工登记兜底 | Git Worker 合约、任务调度/回写、状态保持、前端轮询、真实 GitHub PR #4 与完整测试套件 |
| Provider 合并兼容门禁 | Git Worker、里程碑 16 | Check Runs 可用时逐项校验；PAT 不提供 Checks 时要求 Commit Status 可读、PR clean、reviewed SHA 一致并由 GitHub 分支保护最终裁决 | 403 降级、非 403 拒绝、PR clean、head 漂移拒绝单测；真实 PR #4 squash merge |
| 角色协作与任务交接可视化 | 产品需求、里程碑 17 | AgentRun 与关键 Timeline 事件合并为六角色泳道；按完成时间排序；显式显示交接方向、交付物语义、返工回流和平台动作；长错误及完整审计按需展开 | TypeScript、ESLint、生产构建、SSR 文案与结构断言通过；真实 REQ-001 数据接口与部署服务检查通过 |
| 页面管理 Provider Token 与编辑仓库 | 系统架构、里程碑 15 | 管理员 write-only Secret API、独立 Secret Volume、Git Worker 动态读取；仓库 PUT 与活跃引用身份保护 | 文件权限/原子写、安全边界/RBAC/审计测试；仓库更新与冲突测试；前端构建与文案测试 |
| 开发循环收敛 | Agent Runtime、里程碑 11 | 命令能力说明、重复操作告警、剩余步骤策略、实际修改仓库校验 | 脚本化预算耗尽回归；DeepSeek 5 次请求完成修改、验证和 finish |
