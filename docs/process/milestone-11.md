# 里程碑 11：Agent 失败可诊断与开发循环收敛

- 日期：2026-08-06
- 状态：本地完成并已部署到开发环境

## 现场问题

REQ-001 的仓库工作区和开发分支已经准备成功，但开发工程师运行约 30 个模型步骤后失败。需求卡片只显示“已阻塞”，Agent 运行记录只显示 `agent.invalid_output`，用户无法判断失败阶段、具体原因或应选择哪种恢复动作。

## 根因

1. 开发工具循环允许的命令集合没有传给模型。架构方案给出的 `find`、`grep`、`git` 和 `test` 命令会被沙箱拒绝，模型可能重复尝试并耗尽步骤。
2. Worker 捕获 `AgentOutputError` 时只回传错误码，异常文本、动作轨迹和已消耗 token 全部丢失。
3. `agent_runs` 没有错误详情字段，状态迁移的 `reason` 也只保存错误码。
4. 前端没有阻塞诊断区，审计时间线虽有 `reason` 字段却没有渲染其内容。
5. 主机可见工作区位于平台仓库的 `data/` 下，TypeScript 与 ESLint 会误扫描关联项目源码，污染平台自测。

## 修复

- 开发提示明确列出允许命令，并说明如何用文件工具或 Python 完成等价只读验证。
- 开发循环记录实际文件修改、成功验证和最近动作；重复读取/命令达到三次时发出收敛警告。
- 剩余步骤不足时按“先修改、再验证、立即结束”分阶段强制收敛。
- `finish` 必须声明与实际修改路径一致的仓库 ID，避免文字报告与真实工作区不一致。
- 步骤耗尽使用独立错误码 `agent.step_budget_exhausted`，保留最近 20 个动作、文件改动数、成功验证数和 token 用量。
- SQLite 增加 `agent_runs.error_message`（迁移 v4）；Agent/Git Worker 均保存脱敏后的错误详情，时间线同步保存完整失败原因。
- 页面新增中文阻塞诊断、完整错误详情、建议恢复动作、可读运行状态和时间线原因。
- TypeScript 和 ESLint 排除运行数据目录 `data/`。

## 安全处理

错误详情入库和写日志前会压缩空白、限制长度，并脱敏 Bearer Token、`sk-` Key、常见密钥参数和带用户信息的 HTTP URL。API Key 不进入 Agent 上下文、测试输出或文档。

## 自测证据

| 检查 | 结果 |
| --- | --- |
| 后端完整套件 | 69 passed，10 个付费实时用例按默认策略 skipped |
| 开发循环失败测试 | 验证独立错误码、动作轨迹、改动数和 token 保留 |
| 错误链路测试 | Worker → `agent_runs.error_message` → transition reason 完整保留 |
| 脱敏测试 | Bearer、API Key、`sk-` Key 和 URL 用户信息均被替换 |
| DeepSeek 真实开发循环 | 5 次模型请求完成文档修改、1 次验证成功并正常 finish，共 9641 tokens |
| 前端 | TypeScript、ESLint、生产构建、2 个渲染/文案测试全部通过 |
| SQLite 运行升级 | v4 迁移成功，`error_message` 列存在 |
| Compose | 6 个长期服务全部运行；Redis 与 Control Plane 健康；API ready；Web 可访问 |
| 重建后日志 | Control Plane、Agent Worker、Git Worker、Web 无 ERROR、Traceback 或 failed |

真实 DeepSeek 用例只修改了隔离的临时测试工作区，没有提交或推送外部仓库。历史 `agent.invalid_output` 记录生成时尚未保存异常文本，因此页面会明确标注它是旧记录；新失败均可展示完整详情。
