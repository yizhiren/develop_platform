# ForgeFlow AI 开发平台

ForgeFlow 是一个面向团队的多项目、多仓库 AI 开发平台。每个需求由四个专业 Agent 按固定流程协作：需求澄清、架构设计、开发与测试、架构评审、独立验收，最终由项目负责人逐仓确认合并。

当前仓库同时包含：

- `app/`：Next.js 管理界面。
- `backend/`：FastAPI 控制平面、状态机、Agent 与 Git Provider。
- `docs/`：产品、架构、接口、安全、测试、运维、ADR 和过程记录。
- `docker-compose.yml`：Web、API、Worker 和 Redis 的本地运行环境。

## 快速开始

1. 安装并启动 Docker Desktop。
2. 复制 `.env.example` 为 `.env.local`，仅在本地填写密钥。
3. 执行 `./scripts/start-local.sh`（脚本会读取 `.env.local`）。
4. 打开 `http://localhost:3000`，API 文档位于 `http://localhost:8000/docs`。

默认管理员由首次启动配置创建。DeepSeek 真实测试只有在显式设置 `RUN_LIVE_AI_TESTS=1` 时才会运行；普通测试使用确定性的 Fake Provider。

常用验证：

```bash
npm test
./scripts/docker-cli.sh compose run --rm control-plane pytest -m "not live_ai"
python3 scripts/smoke-local.py
./scripts/docker-cli.sh compose --profile tools run --rm backup
./scripts/docker-cli.sh compose --profile tools run --rm restore-verify
./scripts/docker-cli.sh compose --profile tools run --rm recovery-drill
./scripts/docker-cli.sh compose --profile tools run --rm lease-drill
```

## 文档

从 [文档索引](docs/README.md) 开始。任何公共接口、状态机、数据模型或安全边界的修改，都必须同步更新文档、ADR 或里程碑记录。

## 安全提示

不要把模型、GitHub 或 GitLab 密钥提交到仓库。Agent 沙箱不会接触 Git Provider 凭据，Git 写操作由可信 Git Worker 执行。
