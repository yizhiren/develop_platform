# 里程碑 0：计划 Review 与工程初始化

- 日期：2026-08-05
- 状态：完成

## 关键决定

- SQLite WAL 且 Control Plane 独占访问。
- SQLite Outbox + Redis Streams 替代 Temporal。
- Git Worker 与 Agent 沙箱隔离凭据。
- DeepSeek 仅用于显式真实测试，默认使用 Fake Provider。
- 主机只安装 Docker Desktop，其他服务容器化。

## 已完成

- 创建前端工程与文档体系。
- 记录产品、架构、状态机、数据、安全、测试、运维和 ADR 基线。
- Docker Desktop 4.85.0 / Docker Engine 29.6.2（Apple Silicon）验证通过。
- Web、Control Plane、Agent Worker、Git Worker 与 Redis Compose 启动通过。
- SQLite WAL、Outbox、状态机、Provider 与前端构建测试通过。

## 过程问题与修复

1. FastAPI 0.116 对 204 响应体执行严格校验，启动时暴露 logout 路由错误；改为显式 `Response`。
2. 非 root Control Plane 无权写入新建 Docker Volume；新增只运行一次、仅持有 `CHOWN` capability 的 `storage-init`。
3. 仓库审计事件在 ORM 默认 UUID 生成前写入，导致 `resource_id` 为空；事务内先 flush 再记录审计。
4. Agent 与 Git 任务共用 Stream 会让错误 Worker ACK 消息；拆分为两个独立 Stream 和 Consumer Group。
