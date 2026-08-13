# Agent Runtime

| Agent | 写代码 | 执行测试 | Git 写操作 | 确认业务状态 |
| --- | --- | --- | --- | --- |
| 需求澄清者 | 否 | 否 | 否 | 否 |
| 系统架构师 | 否 | 可运行只读检查 | 否 | 否 |
| 开发工程师 | 是，仅工作区 | 是 | 否 | 否 |
| 验收工程师 | 否 | 是 | 否 | 否 |

每个 `AgentRun` 固化角色、模型、提示词版本、工具版本、需求规格版本、方案版本和仓库基线 SHA。Worker 只接收完成本次任务所需的最小上下文。

模型输出先进入版本化 Pydantic Schema；解析失败时最多执行一次格式修复请求。业务产物同时保存 JSON 与 Markdown。

Provider 包括默认测试使用的 `FakeLLMProvider` 和支持 Chat Completions JSON Output 的 `OpenAICompatibleProvider`。DeepSeek 默认模型为 `deepseek-v4-flash`。

模型配置按稳定身份 `agent1` 至 `agent4` 解析，而不是按临时工作流阶段解析：澄清阶段使用 Agent1；方案、修订和 Code Review 使用 Agent2；开发使用 Agent3；验收、逐仓组合回归和最终验收使用 Agent4。每个身份可覆盖 Provider、Base URL、模型和 API Key；未设置的字段回退到共享 `LLM_*`/`DEEPSEEK_API_KEY`。控制平面只接收 Provider/Base URL/模型用于调度记录，不接收角色 API Key；所有 Key 只进入 Agent Worker，并在 Entrypoint 中转成一次性文件后从 PID 1 环境移除。

当前实现使用非流式 JSON Output。开发 Agent 通过应用层工具循环逐步输出一个 `DeveloperAction`，可列目录、读写/精确替换/删除文件、运行白名单测试命令或结束。每个动作都由服务端校验；模型不能直接调用操作系统或 Git。测试命令经 Unix Socket 交给无网络 Sandbox Executor 执行。结束前至少要有一个真实成功的测试命令，最终 `DevelopmentReport.tests` 由执行器用实际结果覆盖，不能由模型自行声称通过。

Agent4 在普通验收、逐仓组合回归和最终验收中使用独立的 `AcceptanceAction` 工具循环。输入清单来自已确认的 `clarification_spec.acceptance_criteria`，验证方法来自每项 `verification_method`、架构方案测试策略和真实仓库内容。Agent4 只能列目录、读取初始干净 checkout 中的文件、运行白名单验证命令或结束，不能使用写文件工具；每次读取或测试必须声明覆盖的 `criterion_ids`，平台返回不可由模型自命名的 `evidence_id`。批准前必须至少有一个 Agent4 独立成功命令，报告必须且只能覆盖全部已确认验收项，每个 `passed` 项必须引用直接关联的成功证据。

方案阶段启动前，可信 Git Worker 对每个目标仓库执行只读 fresh clone，记录目标分支 head SHA、受限文件树、文件类型统计和限额截取的 README、manifest、入口及文档内容，生成 `repository_analysis`。仓库内容始终标记为不可信输入，Agent2 只能据此设计，不能获得 Git 凭据或写权限。

可信 Git Worker 在方案确认后 clone 目标分支、关闭仓库 hooks、创建需求工作分支并产生 `workspace_manifest`。开发完成后，它扫描 Diff、提交、通过已连接仓库的 SSH URL 或临时 HTTPS Header 推送，并创建或更新 PR/MR，产出包含基线 SHA、head SHA、PR/MR 与实际 Diff 的 `delivery_manifest`。所有 Git 网络命令都在 Git Worker 内执行；Agent Worker 只能编辑共享工作区的普通文件。Review Agent 只能在该产物生成后启动。

`workspace_manifest` 生成后先进入联网 Dependency Worker。它只依据仓库根目录的 `package-lock.json`/`npm-shrinkwrap.json`、`pnpm-lock.yaml` 或 `yarn.lock` 选择 `npm ci`、冻结的 pnpm install 或冻结的 Yarn install；默认关闭生命周期脚本，使用独立下载缓存，并且不持有 Git、模型或平台凭据。安装结果保存为 `dependency_manifest`；失败时在 Agent3 启动前阻塞，成功后开发与测试仍由断网 Sandbox Executor 执行。

Compose 将主机 `${HOST_WORKSPACE_ROOT:-./data/workspaces}` 绑定为 `/workspaces`，因此运维人员可以直接查看 `<requirement_id>/<repository_id>/` 下的 checkout。主机 `${HOST_SSH_DIR:-$HOME/.ssh}` 只读挂载到 Git Worker 的 `/home/forgeflow/.ssh`；Agent Worker 和 Sandbox Executor 只挂载工作区，不挂载 SSH 目录。

验收启动前，可信 Git Worker 从已推送工作分支重新 clone，并校验 checkout HEAD 与 `delivery_manifest.head_sha` 完全一致；每次非末仓合并后，组合回归从已合并仓库目标分支和未合并仓库交付分支 fresh clone；最终验收则从全部目标分支重新 clone，并校验合并返回 SHA。平台先复跑开发报告记录的测试，再由 Agent4 在同一干净 checkout 中选择并执行独立验证。工具循环对初始文件做 SHA-256 快照；验证命令若改变、删除初始文件或创建非测试产物，完整性证据失败且不得批准。Control Plane 在结果落库和状态推进前再次校验验收项完整覆盖、直接证据、必选项状态和独立成功命令。任何复跑失败都会强制把 `AcceptanceReport.approved` 改为 `false`，模型不能覆盖执行器结论。跨仓测试目前是在同一需求验证根目录的多个仓库子目录中执行，不包含业务专用 Compose 编排。

仓库文件、Issue、PR 评论和构建输出均视为不可信数据。工具权限由服务端执行器决定，模型文本不能改变权限。
