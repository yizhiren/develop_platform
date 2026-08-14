# Agent Runtime

| Agent | Pi Core 工具 | 写代码 | 执行测试 | Git 写操作 |
| --- | --- | --- | --- | --- |
| 需求澄清者 | 列目录、搜索、分段读文件、提交规格 | 否 | 否 | 否 |
| 系统架构师 | 列目录、搜索、分段读文件、提交方案/评审 | 否 | 否 | 否 |
| 开发工程师 | 浏览、精确文件修改、验证命令、提交开发报告 | 是，仅工作区 | 是 | 否 |
| 验收工程师 | 浏览、独立验证命令、提交验收证据 | 否 | 是 | 否 |

每个 `AgentRun` 固化角色、模型、提示词版本、工具版本、需求规格版本、方案版本和仓库基线 SHA。Worker 只接收完成本次任务所需的最小上下文。

生产环境中的四个非 Fake 角色都通过固定版本的 `@earendil-works/pi-agent-core` 运行持久化工具会话。Node 桥接进程只负责模型会话、连续工具调用和 token 汇总；工具请求经双向 JSONL RPC 回到 Python，Python 才是文件路径、命令白名单、工作区完整性和最终 Schema 的权威执行者。模型只能调用当前角色显式注册的工具，普通文本不能结束任务，必须调用对应的 `finish_*` 工具并通过 Pydantic Schema 与业务门禁。`FakeLLMProvider` 和关闭 `PI_AGENT_CORE_ENABLED` 的兼容路径继续使用原有 Python 循环，便于离线测试和紧急回退。

Provider 包括默认测试使用的 `FakeLLMProvider` 和支持 Chat Completions JSON Output 的 `OpenAICompatibleProvider`。DeepSeek 默认模型为 `deepseek-v4-flash`。

模型配置按稳定身份 `agent1` 至 `agent4` 解析，而不是按临时工作流阶段解析：澄清阶段使用 Agent1；方案、修订和 Code Review 使用 Agent2；开发使用 Agent3；验收、逐仓组合回归和最终验收使用 Agent4。每个身份可覆盖 Provider、Base URL、模型和 API Key；未设置的字段回退到共享 `LLM_*`/`DEEPSEEK_API_KEY`。控制平面只接收 Provider/Base URL/模型用于调度记录，不接收角色 API Key；所有 Key 只进入 Agent Worker，并在 Entrypoint 中转成一次性文件后从 PID 1 环境移除。

开发 Agent 的 Pi 会话可以连续浏览仓库、提交聚焦修改并根据真实测试结果继续修复。每个动作仍由服务端校验；模型不能直接调用操作系统或 Git。写入只能通过显式文件工具，验证命令不能修改工作区，并经 Unix Socket 交给无网络 Sandbox Executor 执行。结束前至少要有一个真实成功的测试命令，最终 `DevelopmentReport.tests`、`files_changed` 和 `repositories_changed` 由执行器依据实际结果覆盖或核对，不能由模型自行声称通过。

Agent4 在普通验收、逐仓组合回归和最终验收中使用独立的 Pi 工具循环。输入清单来自已确认的 `clarification_spec.acceptance_criteria`，验证方法来自每项 `verification_method`、架构方案测试策略和真实仓库内容。Agent4 只能列目录、读取初始干净 checkout 中的文件、运行白名单验证命令或结束，不能使用写文件工具；每次读取或测试必须声明覆盖的 `criterion_ids`，平台返回不可由模型自命名的 `evidence_id`。批准前必须至少有一个 Agent4 独立成功命令，报告必须且只能覆盖全部已确认验收项，每个 `passed` 项必须引用直接关联的成功证据。

需求澄清启动前，可信 Git Worker 对每个目标仓库执行只读 fresh clone，记录目标分支 head SHA 和 `repository_analysis` 清单。预生成的文件树、语言统计和限额摘要只用于导航；Agent1 随后必须在 Pi 会话中主动列目录、搜索并读取真实文件，至少浏览每个关联仓库并读取所有被规格标记为相关的仓库后才允许提交。用户补充需求时平台重新生成 fresh checkout，避免使用已删除或陈旧的分析目录。需求澄清完成后，Agent2 在同一份当前基线中再次主动读取与方案相关的真实声明、调用点、测试和 CI；Code Review 则读取实际开发工作区。仓库内容始终标记为不可信输入，Agent1/Agent2 都没有 Shell、网络、凭据、写权限或 `.git` 访问能力。

可信 Git Worker 在方案确认后 clone 目标分支、关闭仓库 hooks、创建需求工作分支并产生 `workspace_manifest`。开发完成后，它扫描 Diff、提交、通过已连接仓库的 SSH URL 或临时 HTTPS Header 推送，并创建或更新 PR/MR，产出包含基线 SHA、head SHA、PR/MR 与实际 Diff 的 `delivery_manifest`。所有 Git 网络命令都在 Git Worker 内执行；Agent Worker 只能编辑共享工作区的普通文件。Review Agent 只能在该产物生成后启动。

`workspace_manifest` 生成后先进入联网 Dependency Worker，根据仓库锁文件准备常规依赖并保存 `dependency_manifest`。开发与验收命令随后由具备独立公网出口的 Sandbox Executor 执行；Agent 可以按任务需要重新执行任意 npm 命令和 grep/rg 搜索。Executor 不连接应用内部网络，也不持有 Git、模型或平台凭据。

Compose 将主机 `${HOST_WORKSPACE_ROOT:-./data/workspaces}` 绑定为 `/workspaces`，因此运维人员可以直接查看 `<requirement_id>/<repository_id>/` 下的 checkout。主机 `${HOST_SSH_DIR:-$HOME/.ssh}` 只读挂载到 Git Worker 的 `/home/forgeflow/.ssh`；Agent Worker 和 Sandbox Executor 只挂载工作区，不挂载 SSH 目录。

验收启动前，可信 Git Worker 从已推送工作分支重新 clone，并校验 checkout HEAD 与 `delivery_manifest.head_sha` 完全一致；每次非末仓合并后，组合回归从已合并仓库目标分支和未合并仓库交付分支 fresh clone；最终验收则从全部目标分支重新 clone，并校验合并返回 SHA。平台先复跑开发报告记录的测试，再由 Agent4 在同一干净 checkout 中选择并执行独立验证。工具循环对初始文件做 SHA-256 快照；验证命令若改变、删除初始文件或创建非测试产物，完整性证据失败且不得批准。Control Plane 在结果落库和状态推进前再次校验验收项完整覆盖、直接证据、必选项状态和独立成功命令。任何复跑失败都会强制把 `AcceptanceReport.approved` 改为 `false`，模型不能覆盖执行器结论。跨仓测试目前是在同一需求验证根目录的多个仓库子目录中执行，不包含业务专用 Compose 编排。

仓库文件、Issue、PR 评论和构建输出均视为不可信数据。Pi Core 不被当作权限边界：工具权限、路径解析、命令执行、结果 Schema 和工作流状态全部由服务端执行器决定，模型文本不能改变权限。
