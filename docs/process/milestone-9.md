# 里程碑 9：开发提交与 Code Review 证据链

- 日期：2026-08-06
- 状态：本地完成

## 问题

开发完成后原工作流直接把“commit、push、创建 PR”合并为一个动作。这样虽然 Review 能看到发布后的 Diff，但无法表达“开发工程师已经提交、尚未推送”的边界，也没有把工作分支和 commit SHA 设为系统架构师审查的硬性输入。Review 报告虽已保存为制品，开发提示中也缺少逐条落实驳回意见的强约束。

## 实现决策

- 新增 `git.commit_changes` 可信任务，开发结束后先在本地需求分支创建 commit，不执行 push。
- Agent 仍不能访问 `.git`、网络和 SSH Key；可信 Git Worker 从开发工作树重建干净 checkout 后提交，防止测试命令篡改 Git 元数据。
- 新增 `development_commit_manifest`，记录工作分支、基线 SHA、HEAD SHA、Diff 和 Diff SHA-256。
- 系统架构师必须依据该清单审查；缺少分支、SHA 或 Diff 不得批准。
- Review 驳回后的完整 `code_review_report.findings` 进入下一轮开发上下文，开发工程师必须逐条修复并提供测试证据。
- Review 批准后才执行 `git.publish_changes`；push 前再次校验工作区干净、HEAD SHA 和 Diff 摘要均与已审查证据一致。
- 本地运行配置已启用真实仓库自动化，使新工作流对后续需求生效。

## 验证记录

| 验证 | 结果 |
| --- | --- |
| Git 工作区集成测试 | 本地 commit 完成时远端不存在工作分支；批准后远端 SHA 与 Review SHA 完全一致 |
| 工作流上下文测试 | 架构师收到 `work_branch`、`head_sha` 和 `development_commit_manifest.combined_diff` |
| Review 返工测试 | 下一轮开发上下文保留完整 `summary` 与 `findings` |
| Secret/篡改防护 | commit 前扫描新增内容；push 前复核干净工作区、HEAD 与 Diff 摘要 |
