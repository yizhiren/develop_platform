# 里程碑 14：待合并需求自动创建 Pull Request

- 日期：2026-08-06
- 状态：已完成真实 GitHub 外部验收（PR #4）

## 用户问题

SSH-only 发布可以安全推送系统架构师已评审的工作分支，但没有 Provider Token 时只能展示 GitHub Compare 链接并由用户手工创建、登记 PR。对于 Token 后补或发布时 Provider 暂不可用的需求，原流程也不会自动重放已经完成的发布任务。

## 设计决策

继续保持 Secret Boundary：

- `GITHUB_TOKEN`/`GITLAB_TOKEN` 只进入 Git Worker；
- Control Plane 只接收 Compose 根据 Token 是否存在派生的 `GITHUB_API_ENABLED`/`GITLAB_API_ENABLED` 非敏感标志；
- Agent Worker、Sandbox 和 Web 均不接收 Provider Token；
- 手工 PR 编号登记继续作为故障兜底，而不是正常主路径。

## 执行链路

1. Owner 在待合并仓库点击“由画板创建 PR”。
2. Control Plane 校验需求状态、仓库交付状态、工作分支、reviewed head SHA、Owner 权限和 Provider 能力。
3. Control Plane 创建幂等的 `git.create_pull_request` WorkflowTask 与 OutboxEvent。
4. Git Worker 使用独占 Token 调用 Provider `create_or_update_pull_request`，同一 head/base 已有开放 PR 时复用并更新标题与说明。
5. Git Worker 强制比较 Provider 返回的 head SHA 与系统架构师已评审 SHA；不一致则拒绝回写。
6. Control Plane 回写 PR/MR 编号和 URL，需求保持 `awaiting_merge`，等待人工确认合并。
7. 前端轮询任务与仓库交付状态；成功后显示 PR 编号，失败或超时则保留手工登记入口。

## API

- `POST /api/v1/requirements/{requirement_id}/repositories/{link_id}/pull-request`：Owner 发起自动创建，返回异步任务 ID。
- `GET /api/v1/requirements/{requirement_id}/tasks/{task_id}`：返回当前任务或其自动重试任务状态。
- `GET /api/v1/provider-capabilities`：只返回 Provider API 是否启用，不返回 Token。

## 验证记录

- Git Worker Stub 合约：断言 repository、head、base、标题和 PR 回写数据。
- Control Plane：断言 Owner 请求创建正确任务；完成结果回写 PR #57；需求保持待合并。
- 安全校验：缺 Token、错误状态、缺 reviewed SHA、head SHA 不一致均有拒绝路径。
- 前端：TypeScript、ESLint、生产构建和渲染源代码断言通过。
- 后端完整测试套件通过；付费实时模型测试按默认配置跳过。

2026-08-06 使用页面托管的 fine-grained PAT 在私有仓库 `yizhiren/novel2video` 完成外部验收：画板自动创建 PR #4，Provider 返回的 head SHA `799c69f28e22ca0a505cac08af7f3bc3905ed77a` 与已评审提交一致。后续合并与最终验收记录见里程碑 16。
