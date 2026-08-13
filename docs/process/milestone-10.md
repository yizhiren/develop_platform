# 里程碑 10：旧需求重试与工作区门禁加固

- 日期：2026-08-06
- 状态：本地完成

## 现场问题

一个在仓库自动化启用前创建的需求已经因三轮 Review 驳回而阻塞。用户选择“从方案重试”后，方案修订直接启动开发 Agent，没有补建 `workspace_manifest`。开发 Agent 因缺少真实工作区退化为通用模型调用并生成了文字完成报告；随后新增的本地 commit 任务正确地以 `git.workspace_missing` 拒绝执行，需求再次阻塞。

## 根因

- 新建需求的“确认方案”路径会执行 `git.prepare_workspaces`，但旧需求的 `BLOCKED → REPLANNING → DEVELOPING` 恢复路径没有同等前置条件。
- 自动化模式下，Worker 仅在工作区存在时使用开发工具循环；工作区缺失时错误地退化为通用结构化 Agent。
- 人工开始新一轮开发或方案周期时，旧的 Review/验收失败计数没有重置。
- 原测试只覆盖新需求正向流程，没有覆盖“旧数据 + 功能开关变化 + 阻塞恢复”的组合场景。

## 修复

- `revision_ready` 与 `retry_development` 在仓库自动化开启且缺少工作区制品时，先调度 `git.prepare_workspaces`。
- 已存在工作区制品时继续复用，避免验收返工或普通开发重试丢失当前分支内容。
- “从开发重试”重置 Review 失败预算；“从方案重试”同时重置 Review 与验收失败预算。
- Worker 增加 `agent.workspace_missing` 硬门禁：自动化模式下无工作区绝不调用通用开发 Agent。

## 防回归验证

| 场景 | 预期 |
| --- | --- |
| 旧需求从方案重试且无工作区 | 修订方案后先准备工作区，再启动开发 Agent |
| 阻塞需求从开发重试且无工作区 | 直接准备工作区，不启动开发 Agent |
| 阻塞需求已有工作区 | 复用现有工作区并启动开发 Agent |
| 自动化开发任务无工作区 | 返回 `agent.workspace_missing`，禁止文字型假完成 |
| Fake/协议测试模式无工作区 | 保持兼容，可使用结构化开发 Runtime |

本问题采用“先写失败测试、确认红灯、再修复转绿”的顺序处理。完整套件、容器重建和运行态检查在交付前执行并记录。

额外类型检查发现 Cloudflare Worker 类型未进入 TypeScript 配置，已补充固定版本的 `@cloudflare/workers-types` 和 D1 `Env` 声明，使 `tsc --noEmit` 纳入交付自测。依赖审计同时发现 Next.js 16.2.6 运行时依赖链的高危公告；由于自动修复要求跨出当前锁定版本升级至 16.3.x，已记录风险，避免在本次工作流修复中未经兼容性验证强制升级。

## 交付自测结果

| 检查 | 结果 |
| --- | --- |
| 后端非实时完整套件 | 64 passed |
| 本地 Git commit/Review/push SHA 与旧需求恢复关键链路 | 3 passed，使用临时裸仓，无真实外部 push |
| TypeScript | `tsc --noEmit --incremental false` 通过 |
| ESLint | 通过，零错误 |
| 前端生产构建与渲染测试 | 构建通过，2 passed |
| Compose 运行态 | Control Plane、Agent Worker、Git Worker、Sandbox Executor、Web、Redis 全部运行；API ready；Web HTTP 200 |
| 登录鉴权 | 登录 200，`auth/me` 200 |
| Worker 日志 | 重建后无 ERROR、Traceback 或 failed |

交互式浏览器实例在当前执行环境不可用，因此没有宣称真实点击测试通过；页面层由生产构建和服务端渲染测试覆盖。当前真实需求保持阻塞，未在自测中触发外部仓库写入。
