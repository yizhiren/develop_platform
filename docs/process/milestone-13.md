# 里程碑 13：SSH 发布后的人工 PR 闸门

- 日期：2026-08-06
- 状态：已部署并完成自测

## 现场问题

REQ-001 通过 SSH 推送、干净复验和 Agent4 验收后进入 \`awaiting_merge\`。由于没有配置 GitHub Token，发布产物只有远端分支、head SHA 和 Compare 链接，没有 PR 编号。旧页面仍显示“确认合并下一仓”，用户点击后收到原始错误：

\`next repository has no ready pull request and head SHA\`

这不是合并失败，而是页面错误地跳过了“创建并登记 PR”前置条件。

## 修复

- \`awaiting_merge\` 只有在下一仓同时具备 reviewed head SHA 和 PR/MR 编号时才显示“确认合并下一仓”。
- SSH-only 发布显示独立的 Pull Request Gate，展示 reviewed SHA、工作分支和 Provider Compare 链接。
- 用户在 GitHub/GitLab 创建 PR/MR 后，可在画板登记编号；服务端根据仓库 Provider 和配置生成规范 URL。
- 登记接口只允许项目 Owner 在 \`awaiting_merge\` 使用，并校验：
  - RequirementRepository 属于当前需求；
  - 仓库交付状态为 \`ready\`；
  - work branch 与 reviewed head SHA 不可被客户端篡改；
  - PR URL 的协议、主机、仓库路径和编号匹配。
- Delivery Repositories 不再渲染 \`PR #null\`，没有编号时显示“创建 PR”。
- API 错误优先提取结构化 \`error.message\`/\`detail\`，页面不再直接展示整段原始 JSON。
- Provider 能力接口只暴露 Token 是否已配置，不返回密钥。PR 已登记但对应 Token 缺失时，页面隐藏自动合并动作并明确提示 \`GITHUB_TOKEN\`/\`GITLAB_TOKEN\`；后端也会在创建 MergeAttempt 前拒绝请求。

## 自测

- Provider URL：GitHub、GitLab 合法 PR/MR URL通过；错误主机、仓库、编号、凭据和查询参数均拒绝。
- 登记接口：内存 SQLite 中以 Owner 登记 reviewed head，生成规范 GitHub PR URL并持久化。
- 前端：类型检查、ESLint、生产构建、服务端渲染与 PR Gate 文案/条件测试通过。
- 真实 REQ-001 保持 \`awaiting_merge\`，远端 reviewed branch 与 SHA 未被本次界面修复修改。
- 使用真实管理员会话执行负向 PATCH：错误 head SHA 返回可读的 422；再次读取确认 reviewed SHA 与 PR 编号均未变化。
- 控制面和 Web 镜像完成重建并替换运行容器；\`/health/ready\`、Web 首页与容器日志检查正常。

内置浏览器连接仍无可用实例，因此没有伪造 UI 点击结论；使用真实登录 API、接口测试、构建后渲染和运行态 HTTP 进行替代验证。
