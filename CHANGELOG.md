# Changelog

## 0.2.0 - 2026-08-05

- 产品名称由 ForgeFlow 更新为“画板”，保留数据库、队列、Cookie 和 Compose Volume 的内部兼容标识。
- 登录页与工作台侧栏采用木质画架透明 PNG 品牌 Logo，并保留原始手绘质感。
- Agent1 至 Agent4 支持分别配置 Provider、Base URL、模型和 API Key，并支持共享 DeepSeek 配置回退。
- 多角色 Key 在 Worker 启动时转为一次性文件并从 PID 1 环境移除。
- Git 仓库 clone/fetch/push 统一由 Git Worker 执行；支持主机 SSH 只读挂载与主机可见工作区。
- 明确“先选择项目、再添加仓库、最后发布需求”的界面顺序；仓库与需求弹窗持续显示当前项目，并消除快速切换项目时的跨项目数据残留。
- 需求详情新增 Agent1 澄清对话区，直接展示 `open_questions`；需求提出者的回答会持久化并进入下一轮 Agent1 上下文，问题未清零前不能误确认规格。
- 用户界面不再暴露 `agent1` 至 `agent4` 内部标识，统一显示需求澄清师、系统架构师、开发工程师和验收工程师等职责名称。
- 移除重复的“确认需求规格”操作；需求澄清师返回的待回答问题清零后，工作流自动进入系统架构师方案设计。
- 将通用 Human Gate 升级为结构化方案评审区，在开工授权前展示目标架构、逐仓改动、接口/数据库影响、测试、风险和回滚；退回意见会进入下一轮系统架构师上下文。
- 开发交付改为“本地 commit → 架构师 Review → 批准后 push”：Review 明确绑定开发分支、commit SHA 和 Diff 摘要，驳回 findings 会完整回传下一轮开发工程师。
- 修复旧需求启用仓库自动化后从阻塞态重试时跳过工作区准备的问题；重试会重置对应失败预算，无真实工作区时禁止开发 Agent 生成文本型完成报告。
- Agent 失败现在保存脱敏后的具体原因、token 和开发动作轨迹；阻塞详情页直接展示中文诊断、完整错误和建议恢复动作。
- 开发工具循环公开沙箱能力、检测重复操作并在步骤不足时强制收敛；真实 DeepSeek 文档开发用例在 5 次请求内完成修改、验证和结束。
- 真实仓库回放进一步加入重复读取硬上限、大文件防截断、Python 断言式验证和仓库相对路径约束；系统架构师只评审本轮分支、commit、Diff 与测试，不再被旧评审结论污染。
- Git Worker 在失败重试前清理未提交残留；Review 获得提交后文件快照；无 Provider Token 时仍通过 SSH 推送评审分支并提供手工创建 PR 的 Compare 链接。
- SSH-only 发布进入待合并后新增人工 PR 闸门：创建并登记 PR/MR 编号前隐藏合并按钮；服务端校验 reviewed 分支/SHA 与 Provider URL，原始 JSON 错误改为可读提示。
- 自动合并新增 Provider 授权门禁：只暴露 Token 配置状态；缺少 `GITHUB_TOKEN`/`GITLAB_TOKEN` 时前后端均阻止启动合并并显示可执行提示。
- 待合并需求新增“一键创建 PR/MR”：Control Plane 通过 Outbox 调度持有 Token 的 Git Worker，创建或复用同分支 PR，校验 reviewed head SHA 后回写编号与 URL；手工登记保留为备用方式。
- 系统管理员可在“平台设置”中保存、替换或移除 Provider Token；凭据进入受限 Secret Volume且不回显、不落 SQLite/审计。项目 Owner 可编辑既有仓库连接；活跃需求期间仓库身份与 Clone URL禁止变更。
- Agent4 从单次验收判断升级为干净工作区只读工具循环：逐项读取确认验收标准及验证方法，自主检查文件和运行独立测试；通过项必须绑定平台证据，漏验、伪造证据、测试失败或工作区完整性异常由 Worker 与 Control Plane 双重拒绝。

## 2026-08-05

- 建立 ForgeFlow Web、FastAPI 控制平面、SQLite WAL、Redis Streams 与 Docker Compose。
- 实现项目/仓库/需求、RBAC、四 Agent 状态机、人工闸门、版本化产物与审计。
- 实现 GitHub/GitLab Provider、独立 Git Worker、Webhook 去重和顺序合并基础。
- 完成 Fake E2E、DeepSeek 五协议 live test、容器测试及里程碑过程记录。

## Unreleased

- 初始化 ForgeFlow 前端工程、设计文档、ADR 和过程记录。
- 确定 SQLite Control Plane、Redis Streams、四 Agent 与可信 Git Worker 架构。
- 开发工作区新增联网 Dependency Worker：按锁文件准备 Node 依赖，成功后才启动断网开发 Agent；Worker 不挂载 Git/模型/数据库凭据，默认禁用依赖生命周期脚本。
- 需求详情新增“关闭需求”：原因必填并进入审计链路，关闭会终止排队/在途任务且忽略迟到结果，同时保留历史、附件和已有交付物；远端合并执行期间禁止关闭。
- Compose 的 Agent1/2/4 默认模型固定为 OpenAI `gpt-5.6-sol`，Agent3 默认固定为 DeepSeek `deepseek-v4-pro`；缺少部署环境文件时不再静默回退到 Fake Agent。
