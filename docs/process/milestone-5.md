# 里程碑 5：画板品牌与四 Agent 模型配置

- 日期：2026-08-05
- 状态：本地完成

## 目标

- 将用户可见产品名称从 ForgeFlow 更新为“画板”。
- 从用户提供的画架插图生成透明背景 PNG，并用于登录页与工作台侧栏品牌标识。
- Agent1 至 Agent4 可分别配置模型和 API Key。
- 当前环境继续让四个 Agent 共用 DeepSeek 模型与同一个 Key。
- 保留 `forgeflow:*` Redis Key、数据库文件名、Cookie、备份名和 Compose 项目名，避免品牌改名破坏已有运行数据。

## 安全约束

- `.env.example` 不保存真实密码或 Key。
- 控制平面不接收模型 API Key。
- 角色 Key 和共享 Key 都必须在 Agent Worker PID 1 环境中移除，并在读取后删除临时文件。

## 验证记录

| 验证 | 结果 |
| --- | --- |
| 后端非实时模型套件 | 39 passed，10 live tests 默认排除 |
| DeepSeek 实时套件 | 10 passed；包含 Agent1-4 配置请求与真实改代码/pytest/commit/push |
| 角色配置解析 | 四者均为 `deepseek-v4-flash`，共享同一个非空 Key；独立 Key 覆盖单元测试通过 |
| Secret 边界 | 控制平面无模型 Key；Agent Worker PID 1 无共享/角色 Key；临时文件读取后全部删除 |
| 前端品牌 | lint、production build、2 个 SSR 测试通过；运行页面与 metadata 均显示“画板” |
| 页面 Logo | 251×320 RGBA PNG，透明通道有效；登录页与侧栏均引用；运行资源返回 `image/png` |
| 社交分享图 | 1536×1024 PNG，品牌与中文标题检查通过 |
| Compose | 六个常驻服务运行，控制平面健康，现有 SQLite Volume 保持不变 |
