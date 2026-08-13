# 里程碑 12：真实需求回放与评审证据隔离

- 日期：2026-08-06
- 状态：开发环境完成并通过真实需求验证

## 现场问题

REQ-001 在开发工程师步骤耗尽后再次进入阻塞。历史运行只显示 `agent.invalid_output`；修复诊断链路后，真实错误确认是开发 Agent 在 30 步内反复读取仓库、没有形成文件修改，并在最后两步输出了缺少 `python` 可执行文件的 `-c` 命令。

首次修复后的真实 DeepSeek 回放又暴露了三个不能由脚本化测试发现的问题：

1. 模型可能用整文件写入把大型 `AGENTS.md` 截断；
2. 只打印文件内容的 Python 命令会被误计为成功测试；
3. 文档可能写入沙箱工作区 UUID，而不是仓库相对路径。

真实需求完成开发和本地 commit 后，系统架构师评审又错误复述上一轮评审结论，声称本轮没有 Diff、没有架构章节且测试失败。检查评审 `AgentRun.input_json` 后确认，本轮分支、commit SHA、完整 Diff 和成功测试都已传递，但上下文同时包含旧的 `code_review_report`，模型被过期结论锚定。

## 修复

- 开发上下文只保留最新且必要的规格、方案、评审和验收字段，并限制描述、工具观察和对话窗口大小。
- 开发上下文保留最近六条人类讨论（单条限长）；返工时项目负责人对错误 finding 或残留工作区的明确说明不会再被上下文压缩丢弃。
- 同一读取动作最多执行两次；只读探索总量设硬上限，接近预算时强制进入修改、验证和结束阶段。
- 自动修复 `['-c', script]` 为 `['python', '-c', script]`。
- 大于 8 KiB 的已有文件禁止通过短内容整文件覆盖，要求使用精确替换，避免静默截断。
- 大于 2 KiB 的单次精确替换若删除超过 20% 的选中内容也会被拒绝，避免用摘要替换耐久工程规则。
- Markdown 变更在 `finish` 前执行结构门禁：规范化后重复标题、标题后直接进入同级/上级标题的空章节都会被拒绝并返回具体行号。
- Python 验证必须包含 `assert`、`sys.exit` 或显式抛错；纯读取/打印不计为测试。
- 文档只能记录仓库相对路径，禁止写入 `workspace_root`、repository ID、容器路径、临时目录或 UUID。
- 系统架构师评审上下文移除旧 `code_review_report`；旧 findings 只传给开发工程师。评审提示要求以本轮 `development_commit_manifest` 的分支、commit SHA、Diff 和本轮测试为事实，不得给出与证据矛盾的 finding。
- Review 上下文明确标记为 `pre_publish`：批准后才由可信 Git Worker push，未 push 不能成为本阶段 finding；远端验证由发布和验收负责。“保持 Markdown 格式/风格”允许增加需求内容，不等于要求零 Diff。
- 本地 commit 清单为每个变更文本文件保存提交后快照、SHA-256 和字节数（单文件及总量受限，二进制和疑似敏感内容不内联）。Review 同时使用 Diff 与 HEAD 文件快照，避免把基线已有但未出现在 Diff 中的章节误判为缺失。
- 技术失败后选择“从开发重试”时，先由可信 Git Worker 对精确需求工作区执行 `reset --hard HEAD` 与未跟踪文件清理，再启动开发 Agent；保留已完成的本地 commit，同时丢弃失败运行残留。
- 发布顺序调整为先验证 reviewed SHA/Diff、再通过主机 SSH 推送。未配置 GitHub/GitLab Token 时不再在 push 前失败：保存已推送分支、head SHA 和 Compare/创建 PR 链接，`publication_mode=ssh_branch_only`；配置 Token 时继续自动创建或更新 PR。

## 真实回放记录

所有预演都在真实仓库的隔离副本中调用 DeepSeek，不提交、不推送：

| 回放 | 结果 | 处置 |
| --- | --- | --- |
| 1 | 完成但截断大型 `AGENTS.md` | 判定失败，加入大型文件覆盖保护 |
| 2 | 保留原文，但只用 `print(open(...).read())` 验证 | 判定失败，加入验证命令资格判断 |
| 3 | 29 行增量修改、断言式验证通过，但写入工作区 UUID | 判定失败，加入仓库相对路径约束 |
| 真实 REQ-001 | 开发、断言式测试、本地 commit、基于完整快照的 Review、SSH 发布、干净 clone 复验与 Agent4 验收全部通过 | 状态进入 `awaiting_merge` |

后续真实循环发现，只有 Diff 仍不足以判断基线已有章节，评审误报“章节缺失”，开发工程师进而复制已有章节并因重复校验失败耗尽步骤。平台因此新增提交后完整文本快照，并要求开发工程师将“补充证据”类 finding 通过可失败测试和报告回应，不得通过重复写入已有内容回应。

失败的隔离副本均移动到 `/tmp` 留作短期诊断，未触碰真实需求工作区和远端仓库。

## 回归范围

- 开发工具循环：真实修改、步骤耗尽、无效动作、命令修复、重复读取硬阻断、大文件保护、纯打印不计测试。
- 工作流：Review 驳回内容传给开发工程师；开发分支、commit SHA、Diff 传给系统架构师；旧 Review 不进入下一轮 Review。
- 诊断链路：Worker 错误详情脱敏并写入运行记录、状态迁移和页面阻塞诊断。
- 前端：TypeScript、ESLint、生产构建和服务端渲染测试。

## 最终自测证据

| 检查 | 结果 |
| --- | --- |
| 管理员登录 | 真实 `/auth/login` 与 `/auth/me` 成功，随后通过同一 Cookie API 驱动需求重试 |
| 后端完整套件 | 84 个测试收集；74 passed，10 个显式付费实时用例按默认策略 skipped |
| 前端完整套件 | TypeScript、ESLint、生产构建、2 个服务端渲染测试全部通过 |
| 真实开发 commit | `799c69f28e22ca0a505cac08af7f3bc3905ed77a` |
| Review 证据 | commit 清单含完整 Diff、30,456 字节 `AGENTS.md` HEAD 快照、SHA-256；旧 Review 已隔离 |
| Review | 系统架构师批准，未把未 push 或 Diff 未显示的基线内容误判为 blocker |
| SSH 发布 | 远端 `huaban/req-ee97abfa-584` SHA 与 reviewed SHA 完全一致；模式 `ssh_branch_only` |
| 干净复验 | Git Worker 从已推送分支重新 clone，`published_heads` HEAD 与 reviewed SHA 一致 |
| Agent4 | 验收通过，需求进入 `awaiting_merge` |
| 运行态 | Control Plane/Redis 健康，Web 200；6 个长期服务运行；最终部署后日志无 ERROR/Traceback/failed |

浏览器控制插件在本次会话中返回“无可用 in-app Browser 实例”，因此没有伪造 UI 点击结果；登录、详情读取、重试和状态核对均使用页面同源的真实 HTTP API 完成，并结合数据库产物、容器日志、Git 工作区和远端 SHA 交叉验证。
