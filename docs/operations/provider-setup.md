# Git Provider 接入

## 安全原则

- 使用专用机器人/服务账号和专用测试仓。Token 可由系统管理员在“平台设置 → Provider 凭据”中保存，或写入被忽略的 `.env.local`；不要提交到仓库或粘贴到需求讨论。
- Token 设置过期时间、最小仓库范围并定期轮换。页面保存时 Control Plane 只接收一次写入请求并写入受限 Secret Volume，响应和审计不含明文；Git Worker 对该 Volume 只有只读访问。Agent Worker、Sandbox 和 Web 不挂载 Provider Secret。
- Web URL 必须是不带凭据的 HTTPS；clone URL 可以使用无凭据 HTTPS、`git@host:owner/repo.git` 或 `ssh://git@host/owner/repo.git`。主机必须匹配对应 Provider，SSH 用户必须是 `git`。
- 主机 `${HOST_SSH_DIR:-$HOME/.ssh}` 只读挂载给 Git Worker。先在主机执行 `ssh -T git@github.com`（或 GitLab 对应命令）接受 Host Key 并确认私钥可非交互使用。

## GitHub

优先使用 GitHub App installation token；本地试运行可以使用仅授权测试仓的 fine-grained PAT。当前实现需要以下仓库权限：

- `Pull requests: Read and write`：创建、查找和更新 PR；
- `Contents: Read and write`：在人工确认后 squash merge PR；
- `Checks: Read-only`：合并前读取 Check Runs；
- `Commit statuses: Read-only`：合并前读取传统 Commit Status。

部分 GitHub fine-grained PAT 编辑界面不会提供 `Checks` 选项。此时画板使用受控兼容门禁：仍读取 Commit Status，并在合并前重新读取 PR，要求 PR 为 open、Provider 返回 `mergeable=true` 与 `mergeable_state=clean`，且 head SHA 与系统架构师评审 SHA 完全一致；最终带 expected SHA 调用 GitHub merge API，由 GitHub 再次强制执行分支保护。除 check-runs 返回 403 外的错误不会触发该兼容路径。

只授权画板管理的具体仓库，不要选择所有仓库。若组织启用了 Token 审批，还需由组织管理员批准。GitHub 的 [Pull Requests API](https://docs.github.com/en/rest/pulls/pulls) 明确要求创建 PR 使用 `Pull requests: write`；具体权限以 [fine-grained PAT 权限表](https://docs.github.com/en/rest/authentication/permissions-required-for-fine-grained-personal-access-tokens) 和响应中的 `X-Accepted-GitHub-Permissions` 为准。

```dotenv
GITHUB_TOKEN=...
REPOSITORY_AUTOMATION_ENABLED=1
```

推荐的页面配置方式不需要编辑文件或重启服务：使用系统管理员账号登录，点击顶部“平台设置”，在 GitHub 卡片中粘贴 Token 并保存。页面之后只显示配置状态，不能读取原 Token；替换时输入新 Token，移除时删除页面托管文件。

修改 `.env.local` 后必须带 `--env-file` 执行；不要直接运行不带参数的 `docker compose up`，否则 Compose 插值会使用开发默认值：

```bash
docker compose --env-file .env.local up -d --build --force-recreate control-plane git-worker web
```

使用环境变量时，Compose 只把真实 `GITHUB_TOKEN` 传给 Git Worker；Control Plane 只收到由 Token 是否存在派生出的非敏感 `GITHUB_API_ENABLED` 标志。使用页面托管时，Token 写入独立 Docker Volume，Control Plane 挂载为写入端，Git Worker 挂载为只读端。刷新需求详情后，“手工创建并登记”会升级为“由画板创建 PR”。对于 Token 配置前已经进入“待合并”的需求，点击该按钮会补建或复用同一工作分支的 PR，并校验 GitHub 返回的 head SHA 与系统架构师已评审 SHA 一致。

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
