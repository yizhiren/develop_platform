# API 约定

- 基础路径：`/api/v1`。
- JSON 字段使用 `snake_case`。
- 时间使用 UTC ISO-8601。
- 异步外部任务使用服务端生成的稳定幂等键；客户端 `Idempotency-Key` 为后续兼容字段，当前不据此缓存同步 API 响应。
- 状态转换携带 `expected_version`，冲突返回 HTTP 409。

错误响应：

```json
{"error":{"code":"requirement.invalid_transition","message":"当前状态不允许该操作","details":{},"request_id":"..."}}
```

SSE 事件包含 `event_id`、`event_type`、`project_id`、`requirement_id`、`agent_run_id`、`sequence`、`occurred_at` 和 `payload`。

## 已实现端点

- 认证：`POST /auth/login`、`POST /auth/logout`、`GET /auth/me`
- 用户：`GET/POST /users`（管理员）
- 项目：`GET/POST /projects`
- 成员：`GET /projects/{id}/members`、`PUT /projects/{id}/members`
- 仓库：`GET/POST /projects/{id}/repositories`
- 需求：`GET/POST /projects/{id}/requirements`、`GET /requirements/{id}`
- 工作流：`POST /requirements/{id}/transitions`
- 产物与审计：`GET /requirements/{id}/artifacts|evidence|timeline|agent-runs|messages`、`GET /evidence/{id}/download`、`POST /requirements/{id}/messages`、`GET /audit`
- Git：`GET/PATCH /requirements/{id}/repositories/{link_id}`、`POST /webhooks/github|gitlab`

大型证据下载使用登录 Cookie 或 Bearer Token，并按证据所属需求执行项目 RBAC。列表只返回类型、SHA-256、字节数和时间，不暴露服务端存储路径。
