# 里程碑 4：可信预分析、逐仓回归与生产边界收敛

- 日期：2026-08-05
- 状态：本地完成，真实 GitHub/GitLab 写入验收待凭据

## 本轮审计发现

对产品需求逐条回看后，发现已有流程虽然能完成四 Agent 交付，但仍有五个实质缺口：架构方案没有强制读取真实仓库；逐仓合并后直接进入下一次合并；测试子进程与模型 Worker 共用网络；全局并发上限仅停留在设计；大型完整 Diff 没有进入不可变证据层。历史文档还把已经完成的备份恢复和工作区清理写成“待补”。

## 交付内容

1. 方案前可信仓库分析：Git Worker 对目标分支 fresh clone，固化 head SHA、受限文件树、类型统计和关键文本摘录，再启动 Agent2。
2. 逐仓组合回归：非末仓合并后构造“已合并目标分支 + 未合并交付分支”干净 checkout，平台复跑测试并由 Agent4 判断；失败阻断后续合并。
3. 断网 Sandbox Executor：Agent Worker 只负责模型与策略，测试命令通过 Unix Socket 进入 `network_mode: none` 容器；不挂载 DB、Docker Socket 或 Secret。
4. 分布式容量控制：Redis Lua 原子管理 task/requirement lock、全局活动需求 ZSET、心跳和 TTL，默认最多并行两个需求。
5. 不可变大型证据：完整大 Diff 以 SHA-256 内容寻址写入 Artifact Volume，SQLite 记录元数据，UI 经 RBAC 展示和下载。
6. 可重复恢复演练：真实子进程消费消息后被 `SIGKILL`，替代消费者 `XAUTOCLAIM` 并 ACK；另有真实 Redis 并发槽位演练。

## 验证证据

| 验证 | 结果 |
| --- | --- |
| 后端非实时模型套件 | 36 passed，6 live tests 默认排除 |
| DeepSeek 实时套件 | 5 类结构化协议 + 真实改代码/pytest/commit/push，共 6 passed |
| 仓库预分析 | 临时真实 Git 仓库的 head、树与关键文件提取通过 |
| 双仓增量组合回归 | 合并前后真实 clone、分支选择及失败阻断通过 |
| Sandbox 实际断网 | Agent Worker 经 Unix Socket 执行 TCP 连接，Executor 返回失败 |
| 全局并发 | 上限 1 时第二需求被阻塞；释放后取得租约 |
| Worker 崩溃恢复 | `SIGKILL` 后 Pending 1，替代消费者接管并 ACK 后 Pending 0 |
| 不可变证据 | 内容摘要去重、路径逃逸拒绝、大 Diff 外置与 SHA 校验通过 |
| 前端 | lint、production build 与 SSR 测试通过 |
| 完整 Compose/API | 6 个常驻服务运行；控制平面健康；Fake 四 Agent 冒烟到 `awaiting_merge` |
| SQLite 恢复 | 最新在线备份通过 SHA-256、干净恢复、完整性和迁移 `[1,2,3]` 校验 |

## 安全与适用范围

Sandbox Executor 已隔离外部网络和平台资源，但 MVP 仍是单组织共享工作区服务，不宣称提供不可信租户之间的源码机密隔离。真实 Provider Token 不进入 Agent 或 Sandbox；外部测试只接受专用测试仓库和最小权限 Token。

## 唯一外部待验收项

本地环境没有配置 `GITHUB_TOKEN` 或 `GITLAB_TOKEN`，因此不能对真实外部测试仓执行 push、创建 PR/MR、检查 CI 和 merge。HTTP 合约、本地裸仓数据面、DeepSeek 真实编码和所有本地安全/恢复链路均已验证；配置任一 Provider 的测试仓凭据后即可执行最后验收。
