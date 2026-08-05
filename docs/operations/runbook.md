# 开发与运维手册

## 本地开发

主机仅要求 Docker Desktop。复制 `.env.example` 为 `.env.local` 后执行 `./scripts/start-local.sh`。模型和 Git Secret 不写入 Compose 文件。

要启用真实仓库自动化，设置 `REPOSITORY_AUTOMATION_ENABLED=1`，配置对应 Provider Token，并保持 `ALLOW_LOCAL_GIT=0`。`ALLOW_LOCAL_GIT=1` 仅用于临时裸仓测试。工作区位于 Docker `workspaces` Volume，不应映射宿主机源码目录。

## 健康检查

- Web：`/`
- API：`/health/live`、`/health/ready`
- Redis：`redis-cli ping`

## SQLite 备份

- 使用 SQLite 在线备份 API 生成一致快照。
- 对备份执行 `PRAGMA integrity_check`。
- 保存 SHA-256，并按每日、每周策略轮换。
- 恢复时停止 Control Plane，保留损坏文件副本，再替换数据库并校验。

执行在线备份：

```bash
./scripts/docker-cli.sh compose --profile tools run --rm backup
```

在不覆盖运行库的干净临时目录执行恢复验收：

```bash
./scripts/docker-cli.sh compose --profile tools run --rm restore-verify
```

该命令选择最新备份，先校验 `.sha256`，再复制到容器 tmpfs，执行 `PRAGMA integrity_check` 并输出迁移版本。它不会替换线上数据库。

备份写入 `backups` Docker Volume，控制台只输出文件名和 SHA-256，不输出业务数据。

## 恢复、容量与沙箱演练

```bash
./scripts/docker-cli.sh compose --profile tools run --rm recovery-drill
./scripts/docker-cli.sh compose --profile tools run --rm lease-drill
```

第一条命令会启动一次性消费者、读取专用测试消息后对其发送 `SIGKILL`，再由替代消费者通过 `XAUTOCLAIM` 接管并确认；它只操作带随机后缀的演练 Stream。第二条命令验证全局需求并发上限的阻塞与释放，不操作业务需求。

Sandbox Executor 由 Compose 常驻启动并使用 `network_mode: none`。诊断时应同时检查 `agent-worker` 与 `sandbox-executor` 日志和 `/runtime/sandbox.sock`，不要临时给执行器增加 Docker Socket、数据库 Volume 或 Provider/模型密钥。

## 故障顺序

1. 查看健康检查和最近审计事件。
2. 确认 SQLite Volume 可写和完整性。
3. 确认 Redis 可用；Redis 丢失时触发队列重建。
4. 检查过期租约和未发送 Outbox。
5. 检查 Git/模型外部服务错误分类，禁止无上限重试。

技术错误默认最多尝试三次，间隔按 2、4 秒指数退避（上限 60 秒）；每次重试创建新任务 ID。耗尽后需求进入 `BLOCKED`，运维人员应根据 `error_code` 排除根因，再从适当阶段人工恢复。
