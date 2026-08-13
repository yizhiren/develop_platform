# 里程碑 15：页面管理 Provider Token 与编辑既有仓库

- 日期：2026-08-06
- 状态：已部署并完成真实管理员 API 验收

## 现场问题

自动创建 PR 依赖 `GITHUB_TOKEN`，但此前只能由运维人员修改 `.env.local`；工作台没有凭据入口。既有仓库也只有新增和删除 API，页面无法修正默认分支、URL 或仓库元数据。

## Secret 设计

- 新增独立 `provider-secrets` Docker Volume，不属于 SQLite 或备份 Volume。
- System Admin 可通过 `PUT /api/v1/admin/provider-credentials/{provider}` 写入 Token；响应只返回 `configured/source`，不回显 Token。
- Token 文件采用固定 Provider 文件名、目录 `0700`、文件 `0600`、临时文件 `fsync` 后原子替换；拒绝空白、控制字符、异常长度和未知 Provider。
- 审计仅记录 Provider 和 configured 状态，不包含请求 Token。
- Control Plane 挂载写入端；Git Worker 只读挂载并在每次任务开始时动态读取，页面保存后无需重启。
- Web、Agent Worker 和 Sandbox Executor 均不挂载该 Volume。
- 环境变量模式继续兼容；页面状态标明来源，不能从页面删除环境变量 Token。

## 仓库编辑约束

- 新增 `PUT /api/v1/projects/{project_id}/repositories/{repository_id}`，仅项目 Owner 可调用。
- Provider、external ID、full name、Clone URL、Web URL、默认分支仍执行与新增仓库相同的安全校验。
- 被活跃需求引用时禁止修改 Provider、external ID、full name 和 Clone URL，避免后续验收/合并被重定向；允许修改 Web URL 与只影响未来需求的默认分支。
- 引用需求全部完成或取消后允许修改仓库身份。
- UI 的每个仓库卡片新增“编辑”，表单预填已有数据并展示活跃引用限制。

## 测试证据

- Secret Store：原子写入、`0700/0600` 权限、读写删除、未知 Provider、空白与控制字符拒绝。
- RBAC 与保密：System Admin 可管理；普通成员 403；响应模型和审计均无 Token。
- Git Worker：无环境变量时可以动态读取页面托管 Token。
- 仓库编辑：默认分支更新成功；活跃需求下身份变更返回 409；需求完成后身份更新成功。
- 前端 TypeScript、ESLint、生产构建、渲染/源码断言与后端完整套件全部通过。
- 真实管理员登录后，凭据状态读取成功；带尾随空格的伪 Token 返回 422 且配置状态保持不变；既有 `yizhiren/novel2video` 仓库使用原值执行 PUT 成功。
- 运行时 Secret Volume 权限验证为目录 `0700`、UID/GID `10001`；Control Plane 为读写挂载，Git Worker 为只读挂载，Web/Agent/Sandbox 未挂载。

Browser skill 当前没有可用浏览器实例，因此没有声明完成真实 UI 点击；页面层由生产构建、渲染源码断言和真实 API 会话覆盖。
