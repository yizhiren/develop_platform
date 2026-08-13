# 里程碑 16：真实 GitHub PR、合并门禁与最终验收闭环

- 日期：2026-08-06
- 状态：已完成真实外部仓库交付

## 目标

在管理员通过页面配置 GitHub Token 后，从 REQ-001 的 `awaiting_merge` 状态继续，验证自动创建 PR、合并门禁、Provider 合并、合并后最终验收和需求完成的完整闭环。

## 现场发现与修复

1. 页面托管凭据状态为已配置，但 `/provider-capabilities` 仍只读取环境变量。能力接口改为统一调用动态 Provider Secret 判定，并增加回归测试。
2. GitHub 创建 PR 首次返回 422。原错误只保留 `Validation Failed`，无法看到结构化 `errors`。Provider 适配器现在保留并脱敏安全的错误详情，WorkflowTask 将错误码和错误消息持久化并返回页面。实际原因是 Token 缺少 `Contents` 权限；补充后自动创建成功。
3. 当前 fine-grained PAT 页面没有 `Checks` 权限选项，Check Runs API 返回 403，但 Commit Status 与 PR API 可读。兼容门禁仅对该 403 降级，并要求 PR open、`mergeable=true`、`mergeable_state=clean`、head SHA 等于 reviewed SHA；GitHub merge API 继续携带 expected SHA 并执行分支保护。其他 Check Runs 错误仍失败。
4. 一次手工 Compose 重建未带 `.env.local`，导致 Control Plane 创建的 AgentRun 初始模型元数据为 `fake`，但 Agent Worker 实际使用 DeepSeek 并返回 14,881 tokens。结果处理现会用 Provider 响应的实际模型回填；服务使用 `--env-file .env.local` 重建，历史记录已审计修正为 `deepseek-v4-flash`。

## 真实交付证据

- 需求：`REQ-001 / ee97abfa-5843-4864-87b2-ca382250af24`
- 仓库：`yizhiren/novel2video`
- 工作分支：`huaban/req-ee97abfa-584`
- 系统架构师评审 SHA：`799c69f28e22ca0a505cac08af7f3bc3905ed77a`
- GitHub PR：[#4](https://github.com/yizhiren/novel2video/pull/4)
- Provider 状态：`closed / merged=true`
- Squash merge SHA：`db5cca9397c1c81c0ff5ecaa22c4c4241a9bf8b3`
- 最终验收：Agent4 通过全部 5 条验收标准及记录测试，需求由 `final_acceptance` 转为 `completed`。

## 自测证据

- Provider/Workflow 针对性测试 39 项通过。
- 后端完整套件通过：70% 前 10 项 live tests 按默认策略跳过，其余全部通过。
- 前端 TypeScript、ESLint、生产构建与 2 项 SSR 渲染测试通过。
- 新增覆盖：动态凭据能力、GitHub 结构化错误、任务错误透传、Check Runs 403 兼容路径、PR clean 门禁、head SHA 漂移拒绝、实际模型回填。
- 部署后同时检查 Control Plane 与 Agent Worker：四个稳定 Agent 身份均解析为 `deepseek / deepseek-v4-flash`；API Key 仅 Agent Worker 可见。
