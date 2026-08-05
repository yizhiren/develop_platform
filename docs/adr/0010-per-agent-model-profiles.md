# ADR-0010：四 Agent 独立模型配置

- 状态：接受
- 日期：2026-08-05

## 背景

四个 Agent 的职责、上下文长度和质量要求不同，单一全局模型无法独立优化成本、速度和能力；将 API Key 保存在控制平面或数据库又会扩大 Secret 暴露面。

## 决策

- 配置绑定稳定身份 `agent1` 至 `agent4`；架构修订和 Review 继续归属 Agent2，组合回归和最终验收继续归属 Agent4。
- 每个身份可通过环境变量覆盖 OpenAI-compatible Provider、Base URL、模型和 API Key；任一空字段逐项回退到共享配置。
- 控制平面只接收非秘密的 Provider、Base URL 和模型，用于创建可审计的 `AgentRun.model`。
- Agent Worker 接收共享及角色 Key；Entrypoint 把非空 Key 写入权限为 `0600` 的一次性文件，清除环境变量，Worker 启动时读取并立即删除。
- 当前四个身份显式使用 DeepSeek 同一模型，角色 API Key 留空并继承唯一的共享 `DEEPSEEK_API_KEY`。

## 后果

可以单独替换某个 Agent 的模型或密钥而不影响其他角色，历史运行记录能显示实际计划使用的模型。配置变更需要重新创建控制平面和 Agent Worker；Key 不进入 SQLite、前端 API、日志或示例文件。
