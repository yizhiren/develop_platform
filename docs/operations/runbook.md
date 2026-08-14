# 开发与运维手册

## 本地开发

主机仅要求 Docker Desktop。复制 `.env.example` 为 `.env.local` 后执行 `./scripts/start-local.sh`。模型和 Git Secret 不写入 Compose 文件。

要启用真实仓库自动化，设置 `REPOSITORY_AUTOMATION_ENABLED=1`，配置对应 Provider Token，并保持 `ALLOW_LOCAL_GIT=0`。`ALLOW_LOCAL_GIT=1` 仅用于临时裸仓测试。Git 网络操作全部在 `git-worker` 执行。

本地默认挂载如下：

```dotenv
HOST_WORKSPACE_ROOT=./data/workspaces
HOST_SSH_DIR=/Users/your-name/.ssh
```

`HOST_WORKSPACE_ROOT` 相对 Compose 项目目录解析，当前默认绝对位置是 `/Users/qiming/code/develop_platform/data/workspaces`。目录结构为 `<requirement_id>/<repository_id>/`，另有 `.publishing/` 可信发布副本和 `.leases/` 租约。该目录被 Git 忽略，但包含真实业务源码；不要放入云盘同步目录。

`HOST_SSH_DIR` 只读挂载到 `git-worker:/home/forgeflow/.ssh`。Agent Worker 和 Sandbox Executor 不挂载它。启动前执行 `ssh -T git@github.com` 并确认 `known_hosts`、私钥权限和非交互认证可用；有密码且未通过 Agent 解锁的私钥会在 `BatchMode` 下失败。

## 四 Agent 模型配置

共享默认配置使用 `LLM_PROVIDER`、`LLM_BASE_URL`、`LLM_MODEL` 和 `DEEPSEEK_API_KEY`。每个稳定 Agent 身份可以用以下前缀独立覆盖：

| 身份 | 阶段 | 配置前缀 |
| --- | --- | --- |
| Agent1 | 需求澄清 | `AGENT1_LLM_` |
| Agent2 | 架构、方案修订、Code Review | `AGENT2_LLM_` |
| Agent3 | 开发与测试 | `AGENT3_LLM_` |
| Agent4 | 验收、组合回归、最终验收 | `AGENT4_LLM_` |

每个前缀支持 `PROVIDER`、`BASE_URL`、`MODEL`、`API_KEY`。角色字段为空时逐项回退到共享配置，因此当前只需在 `.env.local` 保存一次 `DEEPSEEK_API_KEY`，四个角色即可使用同一个 Key。若某一角色需要独立 Key，只设置对应的 `AGENTn_LLM_API_KEY`。修改后重新创建 `control-plane` 和 `agent-worker`；Key 不得写入 `.env.example`。

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

Sandbox Executor 由 Compose 常驻启动并连接独立公网出口网络。诊断时应同时检查 `agent-worker` 与 `sandbox-executor` 日志和 `/runtime/sandbox.sock`；不要把执行器接入应用内部网络，也不要增加 Docker Socket、数据库 Volume 或 Provider/模型密钥。

Sandbox 的实际内存由容器级 `SANDBOX_EXECUTOR_MEMORY_LIMIT` 统一约束，默认 `6g`。不要对子进程设置较小的 `RLIMIT_AS`：Node/WebAssembly 会预留较大的虚拟地址空间，即使常驻内存不高也可能被误判为 OOM。调整该值后需替换 `sandbox-executor`。

## 故障顺序

1. 查看健康检查和最近审计事件。
2. 确认 SQLite Volume 可写和完整性。
3. 确认 Redis 可用；Redis 丢失时触发队列重建。
4. 检查过期租约和未发送 Outbox。
5. 检查 Git/模型外部服务错误分类，禁止无上限重试。

技术错误默认最多尝试三次，间隔按 2、4 秒指数退避（上限 60 秒）；每次重试创建新任务 ID。耗尽后需求进入 `BLOCKED`，运维人员应根据 `error_code` 排除根因，再从适当阶段人工恢复。
