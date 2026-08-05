# Changelog

## 0.2.0 - 2026-08-05

- 产品名称由 ForgeFlow 更新为“画板”，保留数据库、队列、Cookie 和 Compose Volume 的内部兼容标识。
- 登录页与工作台侧栏采用木质画架透明 PNG 品牌 Logo，并保留原始手绘质感。
- Agent1 至 Agent4 支持分别配置 Provider、Base URL、模型和 API Key，并支持共享 DeepSeek 配置回退。
- 多角色 Key 在 Worker 启动时转为一次性文件并从 PID 1 环境移除。
- Git 仓库 clone/fetch/push 统一由 Git Worker 执行；支持主机 SSH 只读挂载与主机可见工作区。

## 2026-08-05

- 建立 ForgeFlow Web、FastAPI 控制平面、SQLite WAL、Redis Streams 与 Docker Compose。
- 实现项目/仓库/需求、RBAC、四 Agent 状态机、人工闸门、版本化产物与审计。
- 实现 GitHub/GitLab Provider、独立 Git Worker、Webhook 去重和顺序合并基础。
- 完成 Fake E2E、DeepSeek 五协议 live test、容器测试及里程碑过程记录。

## Unreleased

- 初始化 ForgeFlow 前端工程、设计文档、ADR 和过程记录。
- 确定 SQLite Control Plane、Redis Streams、四 Agent 与可信 Git Worker 架构。
