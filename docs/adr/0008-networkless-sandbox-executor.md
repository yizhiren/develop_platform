# ADR-0008：断网 Sandbox Executor

- 状态：接受
- 日期：2026-08-05

## 背景

Agent Worker 必须访问模型 API，而不可信仓库测试代码必须不能访问外网、平台数据库、Docker Socket 或 Git/模型凭据。在同一个容器内仅清理环境变量不能形成内核级网络边界。

## 决策

- Agent Worker 继续负责模型请求、工具策略和路径/命令校验。
- 测试命令通过受权限保护的 Unix Socket 发送给独立 Sandbox Executor。
- Executor 使用 Compose `network_mode: none`、非 root 用户、只读根文件系统、`cap_drop: ALL`、`no-new-privileges` 和 tmpfs。
- Executor 只挂载工作区与运行时 Socket，不挂载 SQLite、Artifact、Docker Socket或任何 Secret。
- Executor 再次执行工作区根目录、命令白名单、超时、输出上限和进程资源校验。
- MVP 是单组织共享执行器；互不信任的多租户上线前，必须升级为每任务容器/微虚机与独立 Volume。

## 后果

模型网络和测试网络被内核命名空间分离，恶意测试不能通过常规 TCP/UDP 访问外部系统，也无法读取平台 Secret。代价是多一个常驻服务和 Unix Socket 故障点；共享工作区挂载不提供租户间源码保密，因此产品适用范围保持单组织可信成员。
