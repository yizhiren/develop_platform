# 里程碑 2：真实代码交付闭环

- 日期：2026-08-05
- 状态：完成

## 目标

把里程碑 1 中仅有协议的开发阶段升级为可验证执行：目标仓库被真实 clone，开发 Agent 真实修改和测试代码，可信 Git Worker 提交、推送并创建或更新 PR/MR，后续 Review/验收只能使用实际交付证据。

## 关键实现过程

1. 在状态机中加入 `git.prepare_workspaces` 和 `git.publish_changes` 两个可信任务。为兼容早期 Fake E2E，它们受 `REPOSITORY_AUTOMATION_ENABLED` 开关控制。
2. 实现 `GitWorkspaceManager`，为每个需求/仓库建立隔离目录，固定目标分支基线 SHA、关闭 hooks、创建需求工作分支。
3. 实现 `DeveloperToolLoop` 和 `DeveloperAction` Schema。Agent 通过读取、写入、精确替换、删除与白名单命令逐步工作；执行器记录观察结果，模型不能直接获得 Shell。
4. 强制开发 Agent 至少运行一个成功测试，并用执行器结果覆盖报告中的测试字段。
5. 发布阶段执行 Diff Secret 扫描、commit、force-with-lease push 和 PR/MR upsert，生成带实际 Diff 的 `delivery_manifest`；只有该任务成功后才调度架构师 Review。
6. 验收前复跑开发阶段记录的测试；复跑失败时平台强制拒绝验收。
7. 修复 Git Worker 构造时过早创建 `/workspaces` 导致纯 Merge 测试回归的问题，改为仅在 prepare/publish 分支惰性创建工作区管理器。
8. 将可重试技术故障改为新任务 ID + 指数退避，绕开 Worker 完成锁；默认第三次失败后进入 `BLOCKED`。

## 验证证据

| 验证 | 结果 |
| --- | --- |
| 后端非联网测试 | 21 passed，6 live tests 默认排除 |
| 动态编排 | prepare → develop → publish → review，实际 delivery Diff 注入 Review 上下文 |
| 开发工具循环 | Scripted Provider 真实改文件并运行 pytest |
| Git 发布 | 临时裸仓 clone、branch、commit、push、PR Stub、Diff 校验通过 |
| 验收防伪 | 复跑失败可被检测，并强制覆盖模型批准 |
| 技术重试 | 新任务 ID 重投；达到上限后需求进入 BLOCKED |
| DeepSeek 真实闭环 | 真实修复 `add` 实现、pytest 通过、提交并推送需求分支、生成 PR 元数据 |

DeepSeek 完整闭环单次运行约 7 秒。测试使用本地临时裸仓和 Stub PR Provider，不写入用户的 GitHub/GitLab 仓库，也不在日志输出密钥。

## 已知边界

- PR/MR Provider 合约已测，尚未使用用户真实外部仓库做写入测试；启用前需要用户配置专用测试仓和最小权限 Token。
- 验收已使用全新 checkout；最终跨仓组合环境尚未支持由目标项目自定义服务编排。
- Agent Worker 为调用 DeepSeek 具备网络，测试子进程尚未由内核级网络命名空间断网。
- SQLite 仍通过 `create_all` 初始化，版本化迁移尚未引入。
- 工作区容量配额、TTL 清理和依赖缓存策略尚未实现。

## 下一里程碑

完成真实 GitHub/GitLab 测试仓 PR/MR 闭环、干净验收 checkout、跨仓拒绝/重设计/顺序合并端到端、数据库版本化迁移、工作区清理配额和 Redis 故障注入恢复演练。
