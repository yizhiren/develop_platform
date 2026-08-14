# ADR-0011：主机工作区与 SSH Git Worker

- 状态：接受
- 日期：2026-08-05

## 背景

Docker Named Volume 隔离性好，但运维人员不方便直接查看 Agent 正在处理的仓库。与此同时，关联私有仓库需要稳定的 clone/push 凭据，而把主机 SSH 私钥提供给 Agent 容器会破坏既有的凭据边界。

## 决策

1. `git-worker` 是唯一执行远端 Git 命令的容器，承担 clone、ls-remote、fetch 语义的 fresh clone、commit 和 push。
2. `${HOST_SSH_DIR:-$HOME/.ssh}` 只读挂载到 `git-worker:/home/forgeflow/.ssh`。不把 SSH 目录挂载给 Control Plane、Agent Worker 或 Sandbox Executor。
3. `${HOST_WORKSPACE_ROOT:-./data/workspaces}` 绑定挂载为所有代码执行组件共同使用的 `/workspaces`。默认目录位于项目的 `data/workspaces/`，已被 Git 忽略。
4. 允许无凭据 HTTPS、标准 SCP 风格 SSH 和 `ssh://` clone URL；强制 Provider 主机白名单、SSH 用户 `git`、非交互认证及严格 Host Key 校验。
5. SSH 只负责 Git 数据面。PR/MR、CI 状态、Webhook 与 Merge API 仍使用最小权限 Provider Token。

## 结果

- 运维人员可从主机查看 `<requirement_id>/<repository_id>/` 工作副本。
- 主机 SSH Key 不进入镜像、数据库、工作区或 Agent 上下文。
- 主机目录包含关联仓库源码，必须按敏感数据保护并由租约清理器回收。
- 带密码但没有可用 Agent 的私钥会因 `BatchMode` 失败；本地部署必须预先验证 Key 可非交互访问目标仓库。
