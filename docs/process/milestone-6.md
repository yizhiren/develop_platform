# 里程碑 6：主机可见工作区与 SSH Git Worker

- 日期：2026-08-05
- 状态：本地完成

## 目标

- 关联仓库的全部 Git 网络读写都由可信 `git-worker` 执行。
- Git Worker 可只读使用主机 SSH 目录。
- 仓库 checkout 保存到主机明确目录，方便本地查看和故障排查。
- Agent 与测试沙箱继续无法读取 SSH 私钥。

## 目录与容器边界

| 主机/容器 | 路径 | 权限与用途 |
| --- | --- | --- |
| 主机 | `./data/workspaces`（可由 `HOST_WORKSPACE_ROOT` 覆盖） | 查看需求 checkout；Git 忽略 |
| Git Worker | `/workspaces` | clone/commit/push，读写 |
| Agent Worker | `/workspaces` | 只通过服务端文件工具修改普通工作树 |
| Sandbox Executor | `/workspaces` | 无网络测试执行 |
| Git Worker | `/home/forgeflow/.ssh` | 主机 `.ssh` 只读挂载 |

## 验证记录

| 验证 | 结果 |
| --- | --- |
| Compose 路径 | 工作区展开为 `/Users/qiming/code/develop_platform/data/workspaces`；SSH 展开为 `/Users/qiming/.ssh` 且只读 |
| Git Worker 权限 | 主机 Key 可读、工作区可写；GitHub 返回 `Hi yizhiren!` |
| 凭据隔离 | Agent Worker 与 Sandbox Executor 均无 `/home/forgeflow/.ssh` 挂载 |
| SSH 真实 clone | 通过 Git Worker clone `git@github.com:yizhiren/develop_platform.git`，主机可直接看到完整 `.git` checkout |
| URL 安全 | HTTPS、SCP SSH、`ssh://` 正例及错误用户、错误主机、密码、路径逃逸、file URL 反例通过 |
| 后端测试 | 52 passed，10 个 live_ai 默认排除 |
| 运行状态 | 六个常驻服务运行；Control Plane/Redis 健康；Web 正常启动 |
