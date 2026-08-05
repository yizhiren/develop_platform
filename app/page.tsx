"use client";

import Image from "next/image";
import { FormEvent, useCallback, useEffect, useMemo, useState } from "react";

type Project = { id: string; key: string; name: string; description: string };
type Repository = { id: string; provider: string; full_name: string; default_branch: string; webhook_status: string };
type RequirementRepository = { id: string; repository_id: string; target_branch: string; work_branch: string | null; pull_request_number: number | null; pull_request_url: string | null; head_sha: string | null; merge_order: number; status: string };
type Requirement = {
  id: string;
  number: number;
  title: string;
  description: string;
  status: string;
  priority: string;
  review_failures: number;
  acceptance_failures: number;
  version: number;
};
type Artifact = { id: string; kind: string; version: number; markdown: string; created_at: string };
type TimelineItem = { id: string; from_status: string; to_status: string; event: string; actor_type: string; reason: string; created_at: string };
type AgentRun = { id: string; agent_key: string; role: string; status: string; model: string; prompt_version: string; token_usage: number; error_code: string | null; created_at: string; completed_at: string | null };
type Evidence = { id: string; kind: string; sha256: string; size_bytes: number; created_at: string };

const API = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

const statusLabel: Record<string, string> = {
  draft: "草稿",
  clarifying: "需求澄清",
  awaiting_clarification: "待确认澄清",
  planning: "方案设计",
  awaiting_plan: "待确认方案",
  developing: "开发中",
  reviewing: "Code Review",
  accepting: "验收中",
  replanning: "方案修订",
  awaiting_merge: "待合并",
  merging: "合并中",
  regression: "组合回归",
  final_acceptance: "最终验收",
  blocked: "已阻塞",
  completed: "已完成",
};

const agents = [
  ["01", "需求澄清者", "把想法变成可验证的需求规格"],
  ["02", "系统架构师", "设计跨仓方案，并负责独立 Code Review"],
  ["03", "开发工程师", "在隔离沙箱中编码、测试并提交"],
  ["04", "验收工程师", "从干净环境逐项验证验收标准"],
];

export default function Home() {
  const [user, setUser] = useState<{ display_name: string; email: string } | null>(null);
  const [projects, setProjects] = useState<Project[]>([]);
  const [project, setProject] = useState<Project | null>(null);
  const [repositories, setRepositories] = useState<Repository[]>([]);
  const [requirements, setRequirements] = useState<Requirement[]>([]);
  const [error, setError] = useState("");
  const [loginBusy, setLoginBusy] = useState(false);
  const [showCreate, setShowCreate] = useState(false);
  const [showProject, setShowProject] = useState(false);
  const [showRepository, setShowRepository] = useState(false);
  const [selectedRequirement, setSelectedRequirement] = useState<Requirement | null>(null);

  const api = useCallback(async function api<T>(path: string, init?: RequestInit): Promise<T> {
    const response = await fetch(`${API}${path}`, {
      credentials: "include",
      headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
      ...init,
    });
    if (!response.ok) throw new Error((await response.text()) || `HTTP ${response.status}`);
    return response.json() as Promise<T>;
  }, []);

  useEffect(() => {
    api<{ display_name: string; email: string }>("/api/v1/auth/me")
      .then(setUser)
      .catch(() => setUser(null));
  }, [api]);

  useEffect(() => {
    if (!user) return;
    api<Project[]>("/api/v1/projects").then((items) => {
      setProjects(items);
      setProject((current) => current ?? items[0] ?? null);
    }).catch((reason) => setError(String(reason)));
  }, [api, user]);

  useEffect(() => {
    if (!project) return;
    Promise.all([
      api<Repository[]>(`/api/v1/projects/${project.id}/repositories`),
      api<Requirement[]>(`/api/v1/projects/${project.id}/requirements`),
    ]).then(([repoItems, requirementItems]) => {
      setRepositories(repoItems);
      setRequirements(requirementItems);
    }).catch((reason) => setError(String(reason)));
  }, [api, project]);

  useEffect(() => {
    if (!project || !requirements.some((item) => !["draft", "awaiting_clarification", "awaiting_plan", "awaiting_merge", "blocked", "cancelled", "completed"].includes(item.status))) return;
    const timer = window.setInterval(() => {
      api<Requirement[]>(`/api/v1/projects/${project.id}/requirements`).then((items) => {
        setRequirements(items);
        setSelectedRequirement((current) => current ? items.find((item) => item.id === current.id) ?? current : null);
      }).catch(() => undefined);
    }, 1500);
    return () => window.clearInterval(timer);
  }, [api, project, requirements]);

  async function runTransition(item: Requirement, event: string, reason = "") {
    const result = await api<{ requirement: Requirement }>(`/api/v1/requirements/${item.id}/transitions`, { method: "POST", body: JSON.stringify({ event, expected_version: item.version, reason }) });
    setRequirements((current) => current.map((entry) => entry.id === item.id ? result.requirement : entry));
    setSelectedRequirement(result.requirement);
  }

  async function login(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setLoginBusy(true);
    setError("");
    const form = new FormData(event.currentTarget);
    try {
      await api("/api/v1/auth/login", {
        method: "POST",
        body: JSON.stringify({ email: form.get("email"), password: form.get("password") }),
      });
      setUser(await api("/api/v1/auth/me"));
    } catch (reason) {
      setError("登录失败，请检查账号、密码和控制平面状态。" + String(reason));
    } finally {
      setLoginBusy(false);
    }
  }

  const active = useMemo(
    () => requirements.filter((item) => !["completed", "cancelled"].includes(item.status)),
    [requirements],
  );
  const blocked = requirements.filter((item) => item.status === "blocked").length;

  if (!user) {
    return (
      <main className="login-shell">
        <section className="login-story">
          <div className="brand-mark">
            <Image src="/huaban-logo.png" alt="画板 Logo" width={251} height={320} priority unoptimized />
          </div>
          <p className="eyebrow">画板 · AI DELIVERY SYSTEM</p>
          <h1>让每个需求，都有一支完整的工程团队。</h1>
          <p className="hero-copy">四个专业 Agent 在可审计的流程里澄清、设计、开发、评审与验收。人负责方向和最终决定，系统负责把过程做扎实。</p>
          <div className="agent-ribbon">
            {agents.map(([number, name]) => <span key={number}><b>{number}</b>{name}</span>)}
          </div>
        </section>
        <section className="login-panel">
          <div className="login-card">
            <p className="eyebrow">CONTROL PLANE</p>
            <h2>进入工作台</h2>
            <p className="muted">首次启动请使用环境变量中配置的管理员账号。</p>
            <form onSubmit={login}>
              <label>邮箱<input name="email" type="email" defaultValue="admin@example.com" required /></label>
              <label>密码<input name="password" type="password" autoComplete="current-password" required minLength={8} /></label>
              <button className="primary-button" disabled={loginBusy}>{loginBusy ? "正在连接…" : "登录控制平面"}</button>
            </form>
            {error && <p className="error-message">{error}</p>}
            <p className="security-note"><span>●</span> 凭据不会进入 Agent 沙箱或项目仓库</p>
          </div>
        </section>
      </main>
    );
  }

  return (
    <main className="workspace">
      <aside className="sidebar">
        <div className="brand">
          <span className="brand-logo"><Image src="/huaban-logo.png" alt="" width={251} height={320} unoptimized /></span>
          <div>画板<small>AI DELIVERY</small></div>
        </div>
        <nav>
          <a className="active" href="#overview"><i>⌂</i>总览</a>
          <a href="#requirements"><i>◇</i>需求</a>
          <a href="#repositories"><i>⑂</i>仓库</a>
          <a href="#agents"><i>✦</i>Agent 运行</a>
          <a href="#audit"><i>≡</i>审计记录</a>
        </nav>
        <div className="system-card"><span className="live-dot" />控制平面在线<small>SQLite · Redis Streams</small></div>
        <div className="profile"><div className="avatar">{user.display_name.slice(0, 1)}</div><div>{user.display_name}<small>{user.email}</small></div></div>
      </aside>

      <section className="content">
        <header className="topbar">
          <div className="project-switcher">
            <span className="project-symbol">{project?.key.slice(0, 2) ?? "--"}</span>
            <select value={project?.id ?? ""} onChange={(event) => setProject(projects.find((item) => item.id === event.target.value) ?? null)}>
              {projects.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}
            </select>
            <span className="access-badge">OWNER</span>
          </div>
          <div className="top-actions"><button className="ghost-button" onClick={() => setShowProject(true)}>＋ 项目</button><button className="ghost-button" onClick={() => setShowRepository(true)} disabled={!project}>⑂ 仓库</button><button className="primary-button compact" onClick={() => setShowCreate(true)} disabled={!project}>＋ 发布需求</button></div>
        </header>

        <div className="canvas" id="overview">
          <div className="welcome-row">
            <div><p className="eyebrow">PROJECT CONTROL ROOM</p><h1>{project ? project.name : "还没有项目"}</h1><p>{project?.description || "创建项目并连接 GitHub / GitLab 仓库，开始第一条 AI 交付流水线。"}</p></div>
            <div className="date-stamp"><b>05</b><span>AUG<br />2026</span></div>
          </div>

          <section className="metrics">
            <Metric label="活跃需求" value={active.length} detail={`${requirements.length} 条总需求`} tone="cyan" />
            <Metric label="关联仓库" value={repositories.length} detail="GitHub / GitLab" tone="violet" />
            <Metric label="等待人工确认" value={requirements.filter((item) => item.status.startsWith("awaiting_")).length} detail="需要负责人决策" tone="amber" />
            <Metric label="阻塞" value={blocked} detail={blocked ? "已触发人工介入" : "流程运行正常"} tone="red" />
          </section>

          <section className="main-grid">
            <div className="panel requirements-panel" id="requirements">
              <div className="panel-heading"><div><p className="eyebrow">DELIVERY PIPELINE</p><h2>需求运行中</h2></div><button className="text-button">查看全部 →</button></div>
              <div className="requirement-list">
                {requirements.length === 0 ? <EmptyState action={() => setShowCreate(true)} /> : requirements.slice(0, 5).map((item) => <RequirementRow key={item.id} item={item} action={() => setSelectedRequirement(item)} />)}
              </div>
            </div>

            <div className="panel agents-panel" id="agents">
              <div className="panel-heading"><div><p className="eyebrow">AGENT CELL</p><h2>四人小队</h2></div><span className="live-label"><i /> READY</span></div>
              <div className="agent-stack">
                {agents.map(([number, name, description], index) => (
                  <article key={number}><span className={`agent-number a${index + 1}`}>{number}</span><div><h3>{name}</h3><p>{description}</p></div><span className="agent-ready">待命</span></article>
                ))}
              </div>
            </div>
          </section>

          <section className="panel repo-panel" id="repositories">
            <div className="panel-heading"><div><p className="eyebrow">SOURCE CONNECTIONS</p><h2>代码仓库</h2></div><button className="secondary-button" onClick={() => setShowRepository(true)} disabled={!project}>连接仓库</button></div>
            <div className="repo-grid">
              {repositories.length === 0 ? <p className="muted">尚未连接仓库。通过 API 或 Provider 设置连接 GitHub / GitLab 后即可创建需求。</p> : repositories.map((repo) => (
                <article key={repo.id}><span className={`provider ${repo.provider}`}>{repo.provider === "github" ? "GH" : "GL"}</span><div><h3>{repo.full_name}</h3><p>默认分支 · {repo.default_branch}</p></div><span className="connected">{repo.webhook_status === "active" ? "已验证" : "待验证"}</span></article>
              ))}
            </div>
          </section>
        </div>
      </section>

      {showCreate && <CreateRequirementModal project={project} repositories={repositories} close={() => setShowCreate(false)} onCreated={(item) => { setRequirements((current) => [item, ...current]); setShowCreate(false); }} api={api} />}
      {showProject && <CreateProjectModal close={() => setShowProject(false)} api={api} onCreated={(item) => { setProjects((current) => [item, ...current]); setProject(item); setShowProject(false); }} />}
      {showRepository && project && <CreateRepositoryModal project={project} close={() => setShowRepository(false)} api={api} onCreated={(item) => { setRepositories((current) => [...current, item]); setShowRepository(false); }} />}
      {selectedRequirement && <RequirementDrawer item={selectedRequirement} repositories={repositories} close={() => setSelectedRequirement(null)} api={api} transition={runTransition} />}
      {error && <button className="toast" onClick={() => setError("")}>{error}</button>}
    </main>
  );
}

function Metric({ label, value, detail, tone }: { label: string; value: number; detail: string; tone: string }) {
  return <article className={`metric ${tone}`}><p>{label}</p><div><strong>{String(value).padStart(2, "0")}</strong><span>↗</span></div><small>{detail}</small></article>;
}

function RequirementRow({ item, action }: { item: Requirement; action: () => void }) {
  const step = ["draft", "clarifying", "awaiting_clarification", "planning", "awaiting_plan", "developing", "reviewing", "accepting", "awaiting_merge", "merging", "completed"].indexOf(item.status);
  const progress = Math.max(4, Math.round(((step + 1) / 11) * 100));
  return <article className="requirement-row" onClick={action} role="button" tabIndex={0}><div className={`priority ${item.priority}`} /><div className="requirement-copy"><p>REQ-{String(item.number).padStart(3, "0")} · {item.priority.toUpperCase()}</p><h3>{item.title}</h3><div className="progress"><span style={{ width: `${progress}%` }} /></div></div><div className="status-cell"><span className={`status ${item.status}`}>{statusLabel[item.status] ?? item.status}</span><small>{progress}% · 查看 →</small></div></article>;
}

function EmptyState({ action }: { action: () => void }) {
  return <div className="empty-state"><span>◇</span><h3>发布第一条需求</h3><p>需求澄清者会先把目标、边界和验收标准梳理清楚。</p><button className="secondary-button" onClick={action}>开始创建</button></div>;
}

function CreateRequirementModal({ project, repositories, close, onCreated, api }: { project: Project | null; repositories: Repository[]; close: () => void; onCreated: (item: Requirement) => void; api: <T>(path: string, init?: RequestInit) => Promise<T> }) {
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const [selectedRepositories, setSelectedRepositories] = useState<string[]>(repositories.map((repo) => repo.id));
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!project || selectedRepositories.length === 0) { setMessage("请至少选择一个目标代码仓库。"); return; }
    const form = new FormData(event.currentTarget);
    setBusy(true);
    try {
      const chosen = selectedRepositories.map((id) => repositories.find((repo) => repo.id === id)).filter((repo): repo is Repository => Boolean(repo));
      const item = await api<Requirement>(`/api/v1/projects/${project.id}/requirements`, { method: "POST", body: JSON.stringify({ title: form.get("title"), description: form.get("description"), priority: form.get("priority"), repositories: chosen.map((repo, index) => ({ repository_id: repo.id, target_branch: repo.default_branch, merge_order: index })) }) });
      const published = await api<{ requirement: Requirement }>(`/api/v1/requirements/${item.id}/transitions`, { method: "POST", body: JSON.stringify({ event: "publish", expected_version: item.version, reason: "从 Web 工作台发布" }) });
      onCreated(published.requirement);
    } catch (reason) { setMessage(String(reason)); } finally { setBusy(false); }
  }
  function toggleRepository(id: string) {
    setSelectedRepositories((current) => current.includes(id) ? current.filter((item) => item !== id) : [...current, id]);
  }
  return <div className="modal-backdrop" role="dialog" aria-modal="true"><form className="modal" onSubmit={submit}><div className="panel-heading"><div><p className="eyebrow">NEW REQUIREMENT</p><h2>发布需求</h2></div><button type="button" className="close-button" onClick={close}>×</button></div><label>需求标题<input name="title" required minLength={3} placeholder="例如：支持项目成员邀请" /></label><label>详细描述<textarea name="description" required minLength={10} rows={6} placeholder="说明目标、用户场景和预期结果。Agent 会继续澄清细节。" /></label><label>优先级<select name="priority" defaultValue="medium"><option value="low">低</option><option value="medium">中</option><option value="high">高</option><option value="critical">紧急</option></select></label><div className="selected-repos"><p>目标仓库与合并顺序</p>{repositories.map((repo) => { const order = selectedRepositories.indexOf(repo.id); return <label className="repo-choice" key={repo.id}><input type="checkbox" checked={order >= 0} onChange={() => toggleRepository(repo.id)} /><span>{repo.full_name}<small>{order >= 0 ? `第 ${order + 1} 个合并 · ${repo.default_branch}` : "不参与本需求"}</small></span></label>; })}</div>{message && <p className="error-message">{message}</p>}<div className="modal-actions"><button type="button" className="ghost-button" onClick={close}>取消</button><button className="primary-button compact" disabled={busy}>{busy ? "正在发布…" : "发布并开始澄清"}</button></div></form></div>;
}

function CreateProjectModal({ close, onCreated, api }: { close: () => void; onCreated: (item: Project) => void; api: <T>(path: string, init?: RequestInit) => Promise<T> }) {
  const [message, setMessage] = useState("");
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    try {
      onCreated(await api<Project>("/api/v1/projects", { method: "POST", body: JSON.stringify({ key: String(form.get("key")).toUpperCase(), name: form.get("name"), description: form.get("description") }) }));
    } catch (reason) { setMessage(String(reason)); }
  }
  return <div className="modal-backdrop" role="dialog" aria-modal="true"><form className="modal small-modal" onSubmit={submit}><div className="panel-heading"><div><p className="eyebrow">NEW PROJECT</p><h2>创建项目</h2></div><button type="button" className="close-button" onClick={close}>×</button></div><label>项目代号<input name="key" required pattern="[A-Za-z][A-Za-z0-9_-]{1,39}" placeholder="PLATFORM" /></label><label>项目名称<input name="name" required minLength={2} placeholder="研发平台" /></label><label>项目说明<textarea name="description" rows={4} /></label>{message && <p className="error-message">{message}</p>}<div className="modal-actions"><button type="button" className="ghost-button" onClick={close}>取消</button><button className="primary-button compact">创建项目</button></div></form></div>;
}

function CreateRepositoryModal({ project, close, onCreated, api }: { project: Project; close: () => void; onCreated: (item: Repository) => void; api: <T>(path: string, init?: RequestInit) => Promise<T> }) {
  const [message, setMessage] = useState("");
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    const fullName = String(form.get("full_name"));
    try {
      onCreated(await api<Repository>(`/api/v1/projects/${project.id}/repositories`, { method: "POST", body: JSON.stringify({ provider: form.get("provider"), external_id: form.get("external_id") || fullName, full_name: fullName, clone_url: form.get("clone_url"), web_url: form.get("web_url"), default_branch: form.get("default_branch") }) }));
    } catch (reason) { setMessage(String(reason)); }
  }
  return <div className="modal-backdrop" role="dialog" aria-modal="true"><form className="modal" onSubmit={submit}><div className="panel-heading"><div><p className="eyebrow">SOURCE CONNECTION</p><h2>连接代码仓库</h2></div><button type="button" className="close-button" onClick={close}>×</button></div><label>Provider<select name="provider"><option value="github">GitHub</option><option value="gitlab">GitLab</option></select></label><label>仓库全名<input name="full_name" required placeholder="organization/repository" /></label><label>Provider 外部 ID<input name="external_id" placeholder="GitHub 可填写仓库数字 ID；留空时使用全名" /></label><label>Clone URL<input name="clone_url" type="url" required placeholder="https://github.com/org/repo.git" /></label><label>Web URL<input name="web_url" type="url" required placeholder="https://github.com/org/repo" /></label><label>默认分支<input name="default_branch" defaultValue="main" required /></label>{message && <p className="error-message">{message}</p>}<div className="modal-actions"><button type="button" className="ghost-button" onClick={close}>取消</button><button className="primary-button compact">保存连接</button></div></form></div>;
}

function RequirementDrawer({ item, repositories, close, api, transition }: { item: Requirement; repositories: Repository[]; close: () => void; api: <T>(path: string, init?: RequestInit) => Promise<T>; transition: (item: Requirement, event: string, reason?: string) => Promise<void> }) {
  const [artifacts, setArtifacts] = useState<Artifact[]>([]);
  const [timeline, setTimeline] = useState<TimelineItem[]>([]);
  const [requirementRepositories, setRequirementRepositories] = useState<RequirementRepository[]>([]);
  const [agentRuns, setAgentRuns] = useState<AgentRun[]>([]);
  const [evidence, setEvidence] = useState<Evidence[]>([]);
  const [reason, setReason] = useState("");
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    Promise.all([api<Artifact[]>(`/api/v1/requirements/${item.id}/artifacts`), api<TimelineItem[]>(`/api/v1/requirements/${item.id}/timeline`), api<RequirementRepository[]>(`/api/v1/requirements/${item.id}/repositories`), api<AgentRun[]>(`/api/v1/requirements/${item.id}/agent-runs`), api<Evidence[]>(`/api/v1/requirements/${item.id}/evidence`)]).then(([artifactItems, timelineItems, repositoryItems, runItems, evidenceItems]) => { setArtifacts(artifactItems); setTimeline(timelineItems); setRequirementRepositories(repositoryItems); setAgentRuns(runItems); setEvidence(evidenceItems); }).catch((error) => setMessage(String(error)));
  }, [api, item.id, item.status]);
  const actions: Record<string, [string, string][]> = {
    draft: [["publish", "发布并澄清"]],
    awaiting_clarification: [["confirm_clarification", "确认澄清"], ["request_more_clarification", "要求补充"]],
    awaiting_plan: [["confirm_plan", "确认方案并开发"], ["request_plan_change", "要求调整方案"]],
    awaiting_merge: [["begin_merge", "确认合并下一仓"]],
    blocked: [["retry_development", "从开发重试"], ["retry_planning", "从方案重试"], ["retry_regression", "重新组合回归"], ["retry_merge", "重新准备合并"]],
  };
  async function act(event: string) {
    setBusy(true); setMessage("");
    try { await transition(item, event, reason); setReason(""); } catch (error) { setMessage(String(error)); } finally { setBusy(false); }
  }
  async function comment(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!reason.trim()) return;
    try { await api(`/api/v1/requirements/${item.id}/messages`, { method: "POST", body: JSON.stringify({ body: reason }) }); setReason(""); setMessage("讨论消息已记录到审计链路。"); } catch (error) { setMessage(String(error)); }
  }
  return <aside className="detail-drawer"><div className="drawer-head"><div><p className="eyebrow">REQ-{String(item.number).padStart(3, "0")}</p><h2>{item.title}</h2></div><button className="close-button" onClick={close}>×</button></div><div className="drawer-body"><span className={`status ${item.status}`}>{statusLabel[item.status] ?? item.status}</span><p className="requirement-description">{item.description}</p><section className="decision-box"><p className="eyebrow">HUMAN GATE</p><textarea value={reason} onChange={(event) => setReason(event.target.value)} rows={3} placeholder="审批意见、调整原因或讨论消息" /><div className="decision-actions">{(actions[item.status] ?? []).map(([event, label]) => <button key={event} className={event.startsWith("confirm") || event === "begin_merge" || event === "publish" ? "primary-button compact" : "secondary-button compact"} disabled={busy} onClick={() => act(event)}>{label}</button>)}</div><form onSubmit={comment}><button className="text-button" disabled={!reason.trim()}>仅记录讨论 →</button></form>{message && <p className={message.includes("已记录") ? "success-message" : "error-message"}>{message}</p>}</section><section><p className="eyebrow section-label">AGENT RUNS</p><div className="agent-runs">{agentRuns.length === 0 ? <p className="muted">尚未启动 Agent。</p> : agentRuns.slice().reverse().map((run) => <article key={run.id}><span className={`run-state ${run.status}`} /> <div><h3>{run.agent_key} · {run.role.replaceAll("_", " ")}</h3><p>{run.model} · prompt {run.prompt_version} · {run.token_usage} tokens</p>{run.error_code && <small>{run.error_code}</small>}</div><b>{run.status}</b></article>)}</div></section><section><p className="eyebrow section-label">DELIVERY REPOSITORIES</p><div className="delivery-repositories">{requirementRepositories.map((link) => { const repo = repositories.find((candidate) => candidate.id === link.repository_id); return <article key={link.id}><b>{link.merge_order + 1}</b><div><h3>{repo?.full_name ?? link.repository_id}</h3><p>{link.work_branch ?? link.target_branch} · {link.head_sha?.slice(0, 8) ?? "等待交付"}</p></div>{link.pull_request_url ? <a href={link.pull_request_url} target="_blank" rel="noreferrer">PR #{link.pull_request_number} ↗</a> : <span>{link.status}</span>}</article>; })}</div></section>{evidence.length > 0 && <section><p className="eyebrow section-label">IMMUTABLE EVIDENCE</p><div className="evidence-list">{evidence.map((entry) => <a key={entry.id} href={`${API}/api/v1/evidence/${entry.id}/download`}><span>{entry.kind.replaceAll("_", " ")}<small>{entry.sha256.slice(0, 16)}… · {Math.ceil(entry.size_bytes / 1024)} KiB</small></span><b>下载 ↗</b></a>)}</div></section>}<section><p className="eyebrow section-label">VERSIONED ARTIFACTS</p>{artifacts.length === 0 ? <p className="muted">Agent 产物生成后会显示在这里。</p> : artifacts.slice().reverse().map((artifact) => <details className="artifact-card" key={artifact.id}><summary><span>{artifact.kind.replaceAll("_", " ")}</span><b>v{artifact.version}</b></summary><pre>{artifact.markdown}</pre></details>)}</section><section><p className="eyebrow section-label">AUDIT TIMELINE</p><div className="timeline">{timeline.slice().reverse().map((entry) => <article key={entry.id}><i /><div><b>{entry.event}</b><p>{entry.from_status} → {entry.to_status}</p><small>{entry.actor_type} · {new Date(entry.created_at).toLocaleString("zh-CN")}</small></div></article>)}</div></section></div></aside>;
}
