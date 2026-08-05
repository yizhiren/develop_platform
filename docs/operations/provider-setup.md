# Git Provider 接入

## 安全原则

- 使用专用机器人/服务账号和专用测试仓，Token 只写入被忽略的 `.env.local`，不要提交到仓库或粘贴到需求讨论。
- Token 设置过期时间、最小仓库范围并定期轮换。Git Worker 是唯一接收 Token 的服务；Control Plane 和 Agent Worker 不接收。
- Web URL 必须是不带凭据的 HTTPS；clone URL 可以使用无凭据 HTTPS、`git@host:owner/repo.git` 或 `ssh://git@host/owner/repo.git`。主机必须匹配对应 Provider，SSH 用户必须是 `git`。
- 主机 `${HOST_SSH_DIR:-$HOME/.ssh}` 只读挂载给 Git Worker。先在主机执行 `ssh -T git@github.com`（或 GitLab 对应命令）接受 Host Key 并确认私钥可非交互使用。

## GitHub

优先使用 GitHub App installation token；本地试运行可以使用仅授权测试仓的 fine-grained PAT。需要读取仓库/Checks/Commit Status，写入 Contents 与 Pull requests，并允许合并测试 PR。GitHub 的 [Pull Requests API](https://docs.github.com/en/rest/pulls/pulls) 说明创建或更新 PR 需要对来源分支有写权限；具体 Token 权限以 [fine-grained PAT 权限表](https://docs.github.com/en/rest/authentication/permissions-required-for-fine-grained-personal-access-tokens) 和响应中的 `X-Accepted-GitHub-Permissions` 为准。

```dotenv
GITHUB_TOKEN=...
REPOSITORY_AUTOMATION_ENABLED=1
```

画板用 `x-access-token` 作为 HTTPS Basic 用户名，通过临时 `http.extraHeader` 传入 Token，不会修改仓库 remote URL。

使用 SSH 数据面时，连接仓库可填写 `git@github.com:owner/repository.git`。SSH Key 负责 clone/push；Token 仍用于创建 PR、查询 Checks 和执行合并，两者职责不同。

## GitLab

优先使用仅绑定测试项目的 Project Access Token。当前实现需要 Git over HTTPS push、Merge Requests API、pipeline 查询与 merge；经典 Token 至少需要 `api` 权限并具备目标项目 Developer/Maintainer 角色。GitLab 官方的 [Token scope 文档](https://docs.gitlab.com/security/tokens/access_token_scopes/)说明 `api` 提供该 Token 范围内的 API 读写能力，[Merge Requests API](https://docs.gitlab.com/api/merge_requests/)定义了创建、更新和带 head SHA 合并接口。

```dotenv
GITLAB_BASE_URL=https://gitlab.example.com
GITLAB_TOKEN=...
REPOSITORY_AUTOMATION_ENABLED=1
```

GitLab.com 使用 `https://gitlab.com`。画板对 API 使用 Bearer Token，对 Git HTTPS 使用 `oauth2` 用户名和临时认证 Header。

GitLab SSH clone URL 使用 `git@gitlab.example.com:group/repository.git`；自定义实例的 SSH Host 必须与 `GITLAB_BASE_URL` Host 一致。

## 上线前检查

1. 在 Provider 创建空白测试仓和最小 CI。
2. 连接仓库，发布只改一行文档并带一个自动化测试的需求。
3. 确认工作分支、PR/MR、head SHA、Diff、CI 状态与控制台显示一致。
4. 人工确认合并，验证目标分支 SHA 和最终验收产物。
5. 删除测试分支、撤销测试 Token，检查画板日志和 Git 配置中没有 Token。
