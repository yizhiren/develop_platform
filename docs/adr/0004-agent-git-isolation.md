# ADR-0004：Agent 与 Git 凭据隔离

- 状态：接受
- 日期：2026-08-05

可信 Git Worker 负责所有需要 Provider 凭据的动作。Agent 容器只修改已准备的工作区，不获得远端凭据，也不能直接 push 或 merge。
