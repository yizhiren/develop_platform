# 威胁模型

| 威胁 | 控制 |
| --- | --- |
| 恶意仓库脚本逃逸 | 测试在 `network_mode: none` 的非特权 Sandbox Executor 中运行；只读根文件系统、专用工作区 Volume、进程资源限制、无 Docker Socket/平台数据库/凭据、最小环境变量 |
| 提示注入获取权限 | 服务端工具白名单，仓库内容标记为不可信，模型不能修改策略 |
| Agent 窃取 Git 凭据 | Git Worker 与 Agent Worker 分离；发布重新 clone 可信 `.git`，不信任 Agent 修改的 remote/filter/hooks |
| Secret 进入日志或提交 | 日志过滤、Secret 扫描、Push 前检查、密钥不进入镜像 |
| Webhook 伪造或重放 | 签名校验、投递 ID 去重、审计 |
| 越权访问项目 | 服务端 RBAC 和项目成员校验 |
| SQLite 损坏 | WAL、本地 Volume、在线备份和恢复演练 |
| Redis 丢失 | SQLite Outbox 重建未完成任务 |

Agent 命令白名单禁止依赖安装、发布、登录和配置动作，子进程不会继承模型或 Git Secret。共享模型 Key 与 Agent1 至 Agent4 的独立 Key 都由 Entrypoint 转为权限受限的短生命周期文件，Worker 读取后立即删除，并从主进程 exec 环境移除；控制平面不接收这些 Key。Agent Worker 可以出网请求 DeepSeek，但仓库测试代码只在通过 Unix Socket 连接的 `network_mode: none` Sandbox Executor 中运行；实测对外 TCP 连接失败。Git Worker 不使用 Agent 接触过的 `.git`：它从控制面保存的 URL fresh clone，只同步拒绝 symlink/特殊文件的普通工作树，再关闭 hooks、扫描 staged Diff 并 Push。

当前 Sandbox Executor 是组织内共享的单机执行服务并挂载同一个工作区 Volume，因此它提供对平台 Secret、数据库、Docker 和外部网络的强边界，而不是租户级机密计算边界。若未来支持互不信任的多租户，需要升级为每任务独立容器/微虚机和独立 Volume。
