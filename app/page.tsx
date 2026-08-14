"use client";

import Image from "next/image";
import {
  ClipboardEvent,
  FormEvent,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";

type Project = { id: string; key: string; name: string; description: string };
type Repository = {
  id: string;
  provider: string;
  external_id: string;
  full_name: string;
  clone_url: string;
  web_url: string;
  default_branch: string;
  webhook_status: string;
};
type RequirementRepository = {
  id: string;
  repository_id: string;
  target_branch: string;
  work_branch: string | null;
  pull_request_number: number | null;
  pull_request_url: string | null;
  head_sha: string | null;
  merge_order: number;
  status: string;
};
type ProviderCapabilities = {
  github_api_enabled: boolean;
  gitlab_api_enabled: boolean;
};
type ProviderCredentialStatus = {
  provider: "github" | "gitlab";
  configured: boolean;
  source: "managed" | "environment" | "none";
};
type WorkflowTaskState = {
  task_id: string;
  task_type: string;
  status: string;
  error_code: string | null;
  error_message: string | null;
  diagnostics?: AgentRunDiagnostics;
};
type WorkflowTask = {
  id: string;
  agent_run_id: string | null;
  task_type: string;
  status: string;
  created_at: string;
};
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
type RequirementAttachment = {
  id: string;
  requirement_id: string;
  filename: string;
  media_type: string;
  sha256: string;
  size_bytes: number;
  created_at: string;
};
type PendingRequirementImage = {
  id: string;
  filename: string;
  media_type: "image/png" | "image/jpeg" | "image/webp";
  size_bytes: number;
  data_base64: string;
  preview_url: string;
};
type Artifact = {
  id: string;
  kind: string;
  version: number;
  content: Record<string, unknown>;
  markdown: string;
  created_at: string;
};
type TimelineItem = {
  id: string;
  from_status: string;
  to_status: string;
  event: string;
  actor_type: string;
  actor_id: string | null;
  reason: string;
  created_at: string;
};
type AgentRun = {
  id: string;
  agent_key: string;
  role: string;
  status: string;
  model: string;
  prompt_version: string;
  token_usage: number;
  error_code: string | null;
  error_message: string | null;
  diagnostics?: AgentRunDiagnostics;
  created_at: string;
  completed_at: string | null;
};
type AgentRunDiagnostics = {
  turns?: number;
  max_turns?: number;
  tool_calls?: number;
  tool_errors?: number;
  tool_call_counts?: Record<string, number>;
  last_stop_reason?: string;
  terminal_tool_called?: boolean;
};
type FailureDiagnostic = {
  title: string;
  summary: string;
  detail: string;
  recoveryEvent: string;
  recoveryLabel: string;
};
type Evidence = {
  id: string;
  kind: string;
  sha256: string;
  size_bytes: number;
  created_at: string;
};
type ConversationMessage = {
  id: string;
  author_type: string;
  author_id: string | null;
  stage: string;
  body: string;
  created_at: string;
};
type RepositoryPlanContent = {
  repository_id: string;
  purpose: string;
  changes: string[];
  test_commands: string[];
  depends_on: string[];
  merge_order: number;
};
type ArchitecturePlanContent = {
  confidence: number;
  current_state: string;
  target_architecture: string;
  data_flow: string[];
  public_interface_changes: string[];
  database_changes: string[];
  repositories: RepositoryPlanContent[];
  security_considerations: string[];
  migration_and_rollback: string[];
  test_strategy: string[];
  risks: string[];
};

const API = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";
const MAX_REQUIREMENT_IMAGES = 5;
const MAX_REQUIREMENT_IMAGE_BYTES = 5 * 1024 * 1024;
const MAX_REQUIREMENT_IMAGES_TOTAL_BYTES = 15 * 1024 * 1024;
const REQUIREMENT_IMAGE_TYPES = new Set([
  "image/png",
  "image/jpeg",
  "image/webp",
]);

function parseApiTimestamp(value: string): Date {
  const hasTimeZone = /(?:Z|[+-]\d{2}:?\d{2})$/i.test(value);
  return new Date(hasTimeZone ? value : `${value}Z`);
}

function readImageDataUrl(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () =>
      typeof reader.result === "string"
        ? resolve(reader.result)
        : reject(new Error("无法读取截图"));
    reader.onerror = () => reject(new Error("无法读取截图"));
    reader.readAsDataURL(file);
  });
}

async function prepareRequirementImage(
  file: File,
  index: number,
): Promise<PendingRequirementImage> {
  if (!REQUIREMENT_IMAGE_TYPES.has(file.type)) {
    throw new Error("截图仅支持 PNG、JPG 或 WebP 格式。");
  }
  if (file.size > MAX_REQUIREMENT_IMAGE_BYTES) {
    throw new Error("每张截图不能超过 5 MB。");
  }
  const mediaType = file.type as PendingRequirementImage["media_type"];
  const extension =
    mediaType === "image/png"
      ? "png"
      : mediaType === "image/webp"
        ? "webp"
        : "jpg";
  const previewUrl = await readImageDataUrl(file);
  const separator = previewUrl.indexOf(",");
  if (separator < 0) throw new Error("截图内容格式无效。");
  const clipboardName = file.name.trim();
  return {
    id: crypto.randomUUID(),
    filename:
      clipboardName && clipboardName !== "image.png"
        ? clipboardName
        : `screenshot-${Date.now()}-${index + 1}.${extension}`,
    media_type: mediaType,
    size_bytes: file.size,
    data_base64: previewUrl.slice(separator + 1),
    preview_url: previewUrl,
  };
}
const SWIMLANE_REFRESH_INTERVAL_MS = 1500;
const NON_RUNNING_REQUIREMENT_STATUSES = new Set([
  "draft",
  "awaiting_clarification",
  "awaiting_plan",
  "awaiting_merge",
  "paused",
  "blocked",
  "cancelled",
  "completed",
]);

function requirementIsRunning(status: string) {
  return !NON_RUNNING_REQUIREMENT_STATUSES.has(status);
}

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
  paused: "已暂停",
  blocked: "已阻塞",
  cancelled: "已关闭",
  completed: "已完成",
};

const agents = [
  ["01", "需求澄清师", "把想法变成可验证的需求规格"],
  ["02", "系统架构师", "设计跨仓方案，并负责独立 Code Review"],
  ["03", "开发工程师", "在隔离沙箱中编码、测试并提交"],
  ["04", "验收工程师", "从干净环境逐项验证验收标准"],
];

const agentRoleLabel: Record<string, string> = {
  clarify: "需求澄清",
  architect: "方案设计",
  develop: "编码实现",
  review: "代码评审",
  accept: "功能验收",
  revise: "方案修订",
  regression: "组合回归",
  final_accept: "最终验收",
};

const repositoryStatusLabel: Record<string, string> = {
  pending: "等待开发",
  committed: "本地已提交 · 待评审",
  ready: "已推送 · 待合并",
  merged: "已合并",
};

const runStatusLabel: Record<string, string> = {
  queued: "等待执行",
  running: "执行中",
  completed: "已完成",
  rejected: "需返工",
  failed: "执行失败",
  cancelled: "已终止",
};

type WorkflowLaneId =
  | "requester"
  | "clarifier"
  | "architect"
  | "developer"
  | "acceptor"
  | "platform";

type WorkflowSwimlaneEvent = {
  id: string;
  lane: WorkflowLaneId;
  title: string;
  status: string;
  timestamp: string;
  meta: string;
  detail: string;
  footer?: string;
  run?: AgentRun;
  transition?: TimelineItem;
};

const workflowLanes: {
  id: WorkflowLaneId;
  code: string;
  name: string;
  responsibility: string;
}[] = [
  { id: "requester", code: "YOU", name: "需求方", responsibility: "确认与反馈" },
  { id: "clarifier", code: "01", name: "需求澄清师", responsibility: "需求规格" },
  { id: "architect", code: "02", name: "系统架构师", responsibility: "方案与评审" },
  { id: "developer", code: "03", name: "开发工程师", responsibility: "编码与测试" },
  { id: "acceptor", code: "04", name: "验收工程师", responsibility: "验收与回归" },
  { id: "platform", code: "SYS", name: "画板 / Git", responsibility: "提交、推送、合并" },
];

const workflowTimelineLabels: Record<string, string> = {
  publish: "发布需求",
  request_more_clarification: "补充需求信息",
  confirm_clarification: "确认需求规格",
  confirm_plan: "批准实现方案",
  request_plan_change: "要求调整方案",
  retry_planning: "要求重新设计",
  retry_clarification: "重新执行需求澄清",
  retry_development: "要求重新开发",
  retry_review: "重新执行代码评审",
  retry_acceptance: "重新准备验收",
  retry_regression: "要求重新回归",
  retry_merge: "重新准备合并",
  begin_merge: "批准合并",
  analysis_ready: "读取仓库现状",
  workspace_ready: "准备开发工作区",
  workspace_restored: "恢复开发工作区",
  dependencies_ready: "准备项目依赖",
  dependency_failed: "项目依赖准备失败",
  changes_committed: "创建本地提交",
  changes_published: "推送评审分支",
  verification_ready: "准备验收环境",
  incremental_verification_ready: "准备组合回归环境",
  all_repositories_merged: "完成仓库合并",
  repository_merged: "完成单仓合并",
  automation_failed: "仓库自动化失败",
  technical_failure: "任务执行失败",
  merge_failed: "合并失败",
};

const platformTimelineEvents = new Set([
  "confirm_plan",
  "analysis_ready",
  "workspace_ready",
  "workspace_restored",
  "dependencies_ready",
  "dependency_failed",
  "changes_committed",
  "changes_published",
  "verification_ready",
  "incremental_verification_ready",
  "all_repositories_merged",
  "repository_merged",
  "automation_failed",
  "technical_failure",
  "merge_failed",
]);

const workflowRunOutcomeLabels: Record<
  string,
  { title: string; status: string; footer: string }
> = {
  review_approved: {
    title: "代码评审 · 已通过",
    status: "completed",
    footer: "评审通过",
  },
  review_rejected: {
    title: "代码评审 · 未通过",
    status: "rejected",
    footer: "已退回开发",
  },
  acceptance_approved: {
    title: "功能验收 · 已通过",
    status: "completed",
    footer: "验收通过",
  },
  acceptance_rejected: {
    title: "功能验收 · 未通过",
    status: "rejected",
    footer: "已退回方案",
  },
  regression_passed: {
    title: "组合回归 · 已通过",
    status: "completed",
    footer: "回归通过",
  },
  regression_failed: {
    title: "组合回归 · 未通过",
    status: "rejected",
    footer: "回归失败",
  },
  final_acceptance_passed: {
    title: "最终验收 · 已通过",
    status: "completed",
    footer: "最终验收通过",
  },
  final_acceptance_failed: {
    title: "最终验收 · 未通过",
    status: "rejected",
    footer: "最终验收失败",
  },
};

const workflowTaskLabels: Record<string, string> = {
  "git.prepare_analysis": "读取仓库上下文",
  "git.prepare_workspaces": "准备开发工作区",
  "git.restore_workspaces": "恢复开发工作区",
  "git.restore_validation_workspace": "恢复验收工作区",
  "dependency.prepare": "准备项目依赖",
  "dependency.prepare_verification": "准备验收依赖",
  "dependency.prepare_incremental_verification": "准备回归依赖",
  "dependency.prepare_final_verification": "准备最终验收依赖",
  "git.commit_changes": "创建本地提交",
  "git.publish_changes": "推送评审分支",
  "git.create_pull_request": "创建 Pull Request",
  "git.prepare_verification": "准备验收环境",
  "git.prepare_incremental_verification": "准备组合回归环境",
  "git.prepare_final_verification": "准备最终验收环境",
  "git.merge_next": "合并下一仓",
};

function laneForAgent(agentKey: string): WorkflowLaneId {
  return (
    {
      agent1: "clarifier",
      agent2: "architect",
      agent3: "developer",
      agent4: "acceptor",
    } as Record<string, WorkflowLaneId>
  )[agentKey] ?? "platform";
}

function handoffLabel(
  previous: WorkflowSwimlaneEvent,
  current: WorkflowSwimlaneEvent,
): string {
  if (previous.lane === current.lane) {
    return current.status === "failed" ? "本角色执行失败" : "本角色继续处理";
  }
  const route = `${previous.lane}->${current.lane}`;
  const labels: Record<string, string> = {
    "requester->clarifier": "需求说明与补充",
    "clarifier->requester": "澄清问题",
    "clarifier->architect": "已确认的需求规格",
    "requester->architect": "方案确认与调整意见",
    "architect->developer": previous.title.includes("评审")
      ? "Code Review 意见 · 返工"
      : "实现方案与约束",
    "developer->architect": "代码、测试与提交 · Review",
    "architect->acceptor": "评审通过的交付物",
    "acceptor->architect": "验收问题 · 重新设计",
    "acceptor->platform": "验收通过 · 等待合并",
    "platform->acceptor": "合并结果 · 最终验收",
    "requester->platform": "人工授权",
    "architect->platform": "高置信度方案 · 自动授权",
    "platform->developer": "自动开工授权",
    "platform->clarifier": "仓库现状、构建配置与需求说明",
    "platform->requester": "故障与恢复请求",
    "developer->platform": "代码变更",
    "platform->architect": "已提交分支与 SHA",
  };
  return labels[route] ?? "任务与上下文交接";
}

function WorkflowSwimlane({
  agentRuns,
  timeline,
  workflowTasks,
}: {
  agentRuns: AgentRun[];
  timeline: TimelineItem[];
  workflowTasks: WorkflowTask[];
}) {
  const events = useMemo<WorkflowSwimlaneEvent[]>(() => {
    const runEvents = agentRuns.map((run) => {
      const outcomeTransition = timeline
        .slice()
        .reverse()
        .find(
          (entry) =>
            entry.actor_id === run.id && workflowRunOutcomeLabels[entry.event],
        );
      const outcome =
        run.status === "completed" && outcomeTransition
          ? workflowRunOutcomeLabels[outcomeTransition.event]
          : undefined;
      const turns = run.diagnostics?.turns;
      const turnUsage = turns
        ? ` · ${turns}/${run.diagnostics?.max_turns ?? 32} 轮`
        : "";
      const activity =
        run.status === "running"
          ? "正在执行 · 上限 32 轮"
          : run.status === "queued"
            ? "等待 Worker"
            : `${run.token_usage.toLocaleString("zh-CN")} tokens${turnUsage}`;
      return {
        id: `run-${run.id}`,
        lane: laneForAgent(run.agent_key),
        title: outcome?.title ?? agentRoleLabel[run.role] ?? "任务执行",
        status: outcome?.status ?? run.status,
        timestamp:
          outcomeTransition?.created_at ?? run.completed_at ?? run.created_at,
        meta: `${run.model} · ${activity}`,
        detail: outcomeTransition?.reason || run.error_message || "",
        footer: outcome?.footer,
        run,
        transition: outcomeTransition,
      };
    });
    const taskEvents = workflowTasks
      .filter(
        (task) =>
          task.agent_run_id === null &&
          (task.status === "queued" || task.status === "running"),
      )
      .map(
        (task) =>
          ({
            id: `task-${task.id}`,
            lane: "platform",
            title: workflowTaskLabels[task.task_type] ?? "平台自动化任务",
            status: task.status,
            timestamp: task.created_at,
            meta:
              task.status === "running"
                ? "画板 / Git 正在执行"
                : "等待可用 Worker",
            detail: "",
          }) satisfies WorkflowSwimlaneEvent,
      );
    const transitionEvents = timeline
      .filter(
        (entry) =>
          entry.actor_type === "user" || platformTimelineEvents.has(entry.event),
      )
      .map((entry) => {
        const failed =
          entry.to_status === "blocked" ||
          entry.event.includes("failed") ||
          entry.event === "technical_failure";
        return {
          id: `transition-${entry.id}`,
          lane: entry.actor_type === "user" ? "requester" : "platform",
          title:
            entry.event === "confirm_plan" && entry.actor_type === "system"
              ? "自动批准实现方案"
              : (workflowTimelineLabels[entry.event] ?? entry.event),
          status: failed ? "failed" : "completed",
          timestamp: entry.created_at,
          meta: `${statusLabel[entry.from_status] ?? entry.from_status} → ${
            statusLabel[entry.to_status] ?? entry.to_status
          }`,
          detail: entry.reason,
          transition: entry,
        } satisfies WorkflowSwimlaneEvent;
      });
    return [...runEvents, ...taskEvents, ...transitionEvents].sort(
      (left, right) =>
        parseApiTimestamp(left.timestamp).getTime() -
        parseApiTimestamp(right.timestamp).getTime(),
    );
  }, [agentRuns, timeline, workflowTasks]);

  if (events.length === 0) return <p className="muted">尚未启动协作流程。</p>;

  return (
    <div className="swimlane-scroll">
      <div className="workflow-swimlane" aria-label="需求协作泳道图">
        <div className="swimlane-head">
          {workflowLanes.map((lane) => (
            <div className={`lane-heading ${lane.id}`} key={lane.id}>
              <b>{lane.code}</b>
              <span>
                <strong>{lane.name}</strong>
                <small>{lane.responsibility}</small>
              </span>
            </div>
          ))}
        </div>
        <div className="swimlane-flow">
          {events.map((event, index) => {
            const laneIndex = workflowLanes.findIndex(
              (lane) => lane.id === event.lane,
            );
            const previous = events[index - 1];
            const previousLaneIndex = previous
              ? workflowLanes.findIndex((lane) => lane.id === previous.lane)
              : laneIndex;
            const start = Math.min(previousLaneIndex, laneIndex);
            const end = Math.max(previousLaneIndex, laneIndex);
            const direction =
              previousLaneIndex === laneIndex
                ? "same"
                : previousLaneIndex < laneIndex
                  ? "right"
                  : "left";
            const diagnostic =
              event.status === "failed"
                ? failureDiagnostic(event.run, event.transition)
                : null;
            return (
              <div className="swimlane-step" key={event.id}>
                {previous && (
                  <div className="handoff-row" aria-label="任务交接">
                    <div
                      className={`handoff-track ${direction}`}
                      style={{ gridColumn: `${start + 1} / ${end + 2}` }}
                    >
                      <b>{handoffLabel(previous, event)}</b>
                    </div>
                  </div>
                )}
                <div className="swimlane-event-row">
                  <article
                    className={`swimlane-event ${event.lane} ${event.status}`}
                    style={{ gridColumn: laneIndex + 1 }}
                  >
                    <header>
                      <span className={`run-state ${event.status}`} />
                      <time dateTime={event.timestamp}>
                        {parseApiTimestamp(event.timestamp).toLocaleString("zh-CN", {
                          month: "2-digit",
                          day: "2-digit",
                          hour: "2-digit",
                          minute: "2-digit",
                        })}
                      </time>
                    </header>
                    <h3>{event.title}</h3>
                    <p>{event.meta}</p>
                    {diagnostic ? (
                      <details className="swimlane-error">
                        <summary>{diagnostic.title}</summary>
                        <p>{diagnostic.summary}</p>
                        <code>{diagnostic.detail}</code>
                      </details>
                    ) : (
                      event.detail && <small>{event.detail}</small>
                    )}
                    <footer>
                      {event.footer ??
                        runStatusLabel[event.status] ??
                        event.status}
                    </footer>
                  </article>
                </div>
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
}

function failureDiagnostic(
  run: AgentRun | undefined,
  transition: TimelineItem | undefined,
): FailureDiagnostic {
  const code =
    run?.error_code || transition?.reason.split(":", 1)[0] || "worker.failed";
  const persistedDetail = run?.error_message || transition?.reason || "";
  const detail = persistedDetail.includes(":")
    ? persistedDetail.slice(persistedDetail.indexOf(":") + 1).trim()
    : persistedDetail === code
      ? "这是一条旧失败记录，当时系统没有保存详细异常文本。"
      : persistedDetail;
  if (
    code === "agent.pi_turn_budget_exhausted" ||
    (code === "agent.pi_bridge_failed" && run?.role === "clarify")
  ) {
    const metrics = run?.diagnostics;
    const diagnosticDetail = metrics?.turns
      ? `${detail}（轮次 ${metrics.turns}/${metrics.max_turns ?? 50}，工具调用 ${metrics.tool_calls ?? 0}，工具错误 ${metrics.tool_errors ?? 0}）`
      : detail;
    const recovery =
      run?.role === "clarify"
        ? ["retry_clarification", "建议：重新执行需求澄清"]
        : run?.role === "review"
          ? ["retry_review", "建议：重新执行代码评审"]
          : run?.role === "regression"
            ? ["retry_regression", "建议：重新执行组合回归"]
            : run?.role === "architect" || run?.role === "revise"
              ? ["retry_planning", "建议：从方案阶段重试"]
              : run?.role === "develop"
                ? ["retry_development", "建议：从开发阶段重试"]
                : ["retry_acceptance", "建议：从验收阶段重试"];
    return {
      title:
        run?.role === "clarify"
          ? "需求澄清师未在轮次上限内提交结论"
          : "Agent 未在轮次上限内提交结论",
      summary:
        `Agent 已获得最多 ${metrics?.max_turns ?? 32} 轮自主调查时间，但没有在上限内调用结构化结果工具。平台已停止自动重试。`,
      detail: diagnosticDetail,
      recoveryEvent: recovery[0],
      recoveryLabel: recovery[1],
    };
  }
  if (code === "agent.step_budget_exhausted") {
    return {
      title: "开发工程师未能在步骤上限内完成",
      summary:
        "工作区已准备好，但开发 Agent 在修改、验证或结束任务前耗尽了工具步骤。",
      detail,
      recoveryEvent: "retry_development",
      recoveryLabel: "建议：从开发重试，复用当前开发分支",
    };
  }
  if (code === "agent.invalid_output") {
    const reviewFailure = run?.role === "review";
    return {
      title: reviewFailure
        ? "代码评审输出未通过平台校验"
        : "开发工程师的输出未通过平台校验",
      summary:
        "Agent 没有返回平台可执行的结构化结果；平台会先在原任务阶段自动重试结构化输出。",
      detail,
      recoveryEvent: reviewFailure ? "retry_review" : "retry_development",
      recoveryLabel: reviewFailure
        ? "建议：重新执行代码评审，无需重跑开发"
        : "建议：从开发重试；若再次失败，查看下方完整错误详情",
    };
  }
  if (code === "agent.invalid_action") {
    return {
      title: "开发工程师连续返回了不可执行的动作",
      summary: "模型响应不符合开发工具协议，平台为避免执行不确定操作而停止了任务。",
      detail,
      recoveryEvent: "retry_development",
      recoveryLabel: "建议：从开发重试；完整详情会显示连续无效输出次数",
    };
  }
  if (code.includes("workspace_missing")) {
    return {
      title: "开发工作区缺失或已失效",
      summary:
        "平台无法找到该需求的本地仓库工作区，因此没有允许开发 Agent 继续修改。",
      detail,
      recoveryEvent: "retry_development",
      recoveryLabel: "建议：从开发重试，平台会先重新准备工作区",
    };
  }
  if (code.startsWith("dependency.")) {
    return {
      title: "项目依赖准备失败",
      summary:
        "联网依赖 Worker 未能根据锁文件完成安装，开发 Agent 尚未启动。",
      detail,
      recoveryEvent: "retry_development",
      recoveryLabel: "建议：确认包仓库可访问后，从开发重试",
    };
  }
  if (code === "model.missing_api_key") {
    return {
      title: "模型 API Key 未配置",
      summary:
        "对应角色已配置为真实模型，但 Agent Worker 没有读取到共享或角色专用 API Key。",
      detail,
      recoveryEvent: "retry_development",
      recoveryLabel: "建议：加载 .env.local 重启 Agent Worker 后，从开发重试",
    };
  }
  if (code.startsWith("model.")) {
    return {
      title: "模型服务调用失败",
      summary: "Agent 阶段因模型超时、网络或服务响应异常而终止。",
      detail,
      recoveryEvent: "retry_development",
      recoveryLabel: "建议：确认模型服务恢复后，从开发重试",
    };
  }
  if (code.startsWith("git.")) {
    const recovery =
      transition?.from_status === "clarifying" ||
      transition?.from_status === "awaiting_clarification"
        ? ["retry_clarification", "建议：重新获取仓库信息并继续需求澄清"]
        : transition?.from_status === "planning" ||
            transition?.from_status === "replanning" ||
            transition?.from_status === "awaiting_plan"
          ? ["retry_planning", "建议：重新获取仓库信息并继续方案设计"]
          : transition?.from_status === "reviewing"
            ? ["retry_review", "建议：检查仓库状态后重新执行代码评审"]
            : transition?.from_status === "accepting"
              ? ["retry_acceptance", "建议：检查仓库状态后重新准备验收"]
              : transition?.from_status === "regression"
                ? ["retry_regression", "建议：检查仓库状态后重新执行组合回归"]
                : transition?.from_status === "merging" ||
                    transition?.from_status === "awaiting_merge"
                  ? ["retry_merge", "建议：检查仓库状态后重新准备合并"]
                  : ["retry_development", "建议：检查仓库状态后重新准备开发"];
    return {
      title: "仓库自动化执行失败",
      summary: "平台在准备、提交或发布代码时遇到 Git 错误。",
      detail,
      recoveryEvent: recovery[0],
      recoveryLabel: recovery[1],
    };
  }
  return {
    title: "自动化流程遇到技术故障",
    summary: "本次任务没有正常完成，详细错误如下。",
    detail: detail || code,
    recoveryEvent: "retry_planning",
    recoveryLabel: "建议：根据错误详情选择重试阶段",
  };
}

export default function Home() {
  const [user, setUser] = useState<{
    id: string;
    display_name: string;
    email: string;
    system_role: string;
  } | null>(null);
  const [projects, setProjects] = useState<Project[]>([]);
  const [project, setProject] = useState<Project | null>(null);
  const [repositories, setRepositories] = useState<Repository[]>([]);
  const [requirements, setRequirements] = useState<Requirement[]>([]);
  const [error, setError] = useState("");
  const [loginBusy, setLoginBusy] = useState(false);
  const [showCreate, setShowCreate] = useState(false);
  const [showProject, setShowProject] = useState(false);
  const [showRepository, setShowRepository] = useState(false);
  const [showSettings, setShowSettings] = useState(false);
  const [editingRepository, setEditingRepository] =
    useState<Repository | null>(null);
  const [selectedRequirement, setSelectedRequirement] =
    useState<Requirement | null>(null);
  const [loadedProjectId, setLoadedProjectId] = useState<string | null>(null);

  const api = useCallback(async function api<T>(
    path: string,
    init?: RequestInit,
  ): Promise<T> {
    const response = await fetch(`${API}${path}`, {
      credentials: "include",
      headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
      ...init,
    });
    if (!response.ok) {
      const text = await response.text();
      let detail = text || `HTTP ${response.status}`;
      try {
        const payload = JSON.parse(text) as {
          detail?: string;
          error?: { message?: string; code?: string };
        };
        detail = payload.error?.message || payload.detail || detail;
      } catch {
        // Keep the plain-text response when the server did not return JSON.
      }
      throw new Error(detail);
    }
    return response.json() as Promise<T>;
  }, []);

  useEffect(() => {
    api<{
      id: string;
      display_name: string;
      email: string;
      system_role: string;
    }>("/api/v1/auth/me")
      .then(setUser)
      .catch(() => setUser(null));
  }, [api]);

  useEffect(() => {
    if (!user) return;
    api<Project[]>("/api/v1/projects")
      .then((items) => {
        setProjects(items);
        setProject((current) =>
          current && items.some((item) => item.id === current.id)
            ? current
            : (items[0] ?? null),
        );
      })
      .catch((reason) => setError(String(reason)));
  }, [api, user]);

  useEffect(() => {
    if (!project) return;
    let cancelled = false;
    Promise.all([
      api<Repository[]>(`/api/v1/projects/${project.id}/repositories`),
      api<Requirement[]>(`/api/v1/projects/${project.id}/requirements`),
    ])
      .then(([repoItems, requirementItems]) => {
        if (cancelled) return;
        setRepositories(repoItems);
        setRequirements(requirementItems);
        setLoadedProjectId(project.id);
      })
      .catch((reason) => {
        if (!cancelled) {
          setError(String(reason));
          setLoadedProjectId(project.id);
        }
      });
    return () => {
      cancelled = true;
    };
  }, [api, project]);

  useEffect(() => {
    if (
      !project ||
      !requirements.some((item) => requirementIsRunning(item.status))
    )
      return;
    let activeRequest = true;
    const timer = window.setInterval(() => {
      api<Requirement[]>(`/api/v1/projects/${project.id}/requirements`)
        .then((items) => {
          if (!activeRequest) return;
          setRequirements(items);
          setSelectedRequirement((current) =>
            current
              ? (items.find((item) => item.id === current.id) ?? current)
              : null,
          );
        })
        .catch(() => undefined);
    }, 1500);
    return () => {
      activeRequest = false;
      window.clearInterval(timer);
    };
  }, [api, project, requirements]);

  async function runTransition(item: Requirement, event: string, reason = "") {
    const result = await api<{ requirement: Requirement }>(
      `/api/v1/requirements/${item.id}/transitions`,
      {
        method: "POST",
        body: JSON.stringify({ event, expected_version: item.version, reason }),
      },
    );
    setRequirements((current) =>
      current.map((entry) =>
        entry.id === item.id ? result.requirement : entry,
      ),
    );
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
        body: JSON.stringify({
          email: form.get("email"),
          password: form.get("password"),
        }),
      });
      setUser(await api("/api/v1/auth/me"));
    } catch (reason) {
      setError("登录失败，请检查账号、密码和控制平面状态。" + String(reason));
    } finally {
      setLoginBusy(false);
    }
  }

  function activateProject(nextProject: Project | null) {
    setProject(nextProject);
    setRepositories([]);
    setRequirements([]);
    setSelectedRequirement(null);
    setShowRepository(false);
    setShowCreate(false);
    setLoadedProjectId(null);
    setError("");
  }

  const active = useMemo(
    () =>
      requirements.filter(
        (item) => !["completed", "cancelled"].includes(item.status),
      ),
    [requirements],
  );
  const blocked = requirements.filter(
    (item) => item.status === "blocked",
  ).length;
  const projectDataLoading = Boolean(project && loadedProjectId !== project.id);
  const canPublishRequirement = Boolean(
    project && repositories.length > 0 && !projectDataLoading,
  );
  const publishRequirementHint = !project
    ? "请先创建或选择项目"
    : projectDataLoading
      ? `正在加载 ${project.name} 的仓库`
      : repositories.length === 0
        ? `请先为 ${project.name} 添加仓库`
        : `在 ${project.name} 中发布需求`;

  if (!user) {
    return (
      <main className="login-shell">
        <section className="login-story">
          <div className="brand-mark">
            <Image
              src="/huaban-logo.png"
              alt="画板 Logo"
              width={251}
              height={320}
              priority
              unoptimized
            />
          </div>
          <p className="eyebrow">画板 · AI DELIVERY SYSTEM</p>
          <h1>让每个需求，都有一支完整的工程团队。</h1>
          <p className="hero-copy">
            四个专业 Agent
            在可审计的流程里澄清、设计、开发、评审与验收。人负责方向和最终决定，系统负责把过程做扎实。
          </p>
          <div className="agent-ribbon">
            {agents.map(([number, name]) => (
              <span key={number}>
                <b>{number}</b>
                {name}
              </span>
            ))}
          </div>
        </section>
        <section className="login-panel">
          <div className="login-card">
            <p className="eyebrow">CONTROL PLANE</p>
            <h2>进入工作台</h2>
            <p className="muted">首次启动请使用环境变量中配置的管理员账号。</p>
            <form onSubmit={login}>
              <label>
                邮箱
                <input
                  name="email"
                  type="email"
                  defaultValue="admin@example.com"
                  required
                />
              </label>
              <label>
                密码
                <input
                  name="password"
                  type="password"
                  autoComplete="current-password"
                  required
                  minLength={8}
                />
              </label>
              <button className="primary-button" disabled={loginBusy}>
                {loginBusy ? "正在连接…" : "登录控制平面"}
              </button>
            </form>
            {error && <p className="error-message">{error}</p>}
            <p className="security-note">
              <span>●</span> 凭据不会进入 Agent 沙箱或项目仓库
            </p>
          </div>
        </section>
      </main>
    );
  }

  return (
    <main className="workspace">
      <aside className="sidebar">
        <div className="brand">
          <span className="brand-logo">
            <Image
              src="/huaban-logo.png"
              alt=""
              width={251}
              height={320}
              unoptimized
            />
          </span>
          <div>
            画板<small>AI DELIVERY</small>
          </div>
        </div>
        <nav>
          <a className="active" href="#overview">
            <i>⌂</i>总览
          </a>
          <a href="#requirements">
            <i>◇</i>需求
          </a>
          <a href="#repositories">
            <i>⑂</i>仓库
          </a>
          <a href="#agents">
            <i>✦</i>Agent 运行
          </a>
          <a href="#audit">
            <i>≡</i>审计记录
          </a>
        </nav>
        <div className="system-card">
          <span className="live-dot" />
          控制平面在线<small>SQLite · Redis Streams</small>
        </div>
        <div className="profile">
          <div className="avatar">{user.display_name.slice(0, 1)}</div>
          <div>
            {user.display_name}
            <small>{user.email}</small>
          </div>
        </div>
      </aside>

      <section className="content">
        <header className="topbar">
          <div className="project-switcher">
            <span className="project-symbol">
              {project?.key.slice(0, 2) ?? "--"}
            </span>
            <div className="project-switcher-copy">
              <small>当前项目</small>
              <select
                aria-label="当前项目"
                value={project?.id ?? ""}
                onChange={(event) =>
                  activateProject(
                    projects.find((item) => item.id === event.target.value) ??
                      null,
                  )
                }
              >
                {projects.length === 0 && (
                  <option value="">请先创建项目</option>
                )}
                {projects.map((item) => (
                  <option key={item.id} value={item.id}>
                    {item.name}
                  </option>
                ))}
              </select>
            </div>
            <span className="access-badge">OWNER</span>
          </div>
          <div className="top-actions">
            {user.system_role === "admin" && (
              <button
                className="ghost-button"
                onClick={() => setShowSettings(true)}
              >
                ⚙ 平台设置
              </button>
            )}
            <button
              className="ghost-button"
              onClick={() => setShowProject(true)}
            >
              ＋ 新建项目
            </button>
            <button
              className="ghost-button"
              onClick={() => setShowRepository(true)}
              disabled={!project}
              title={project ? `添加到 ${project.name}` : "请先创建或选择项目"}
            >
              ⑂ 添加仓库
            </button>
            <button
              className="primary-button compact"
              onClick={() => setShowCreate(true)}
              disabled={!canPublishRequirement}
              title={publishRequirementHint}
            >
              ＋ 发布需求
            </button>
          </div>
        </header>

        <div className="canvas" id="overview">
          <div className="welcome-row">
            <div>
              <p className="eyebrow">PROJECT CONTROL ROOM</p>
              <h1>{project ? project.name : "还没有项目"}</h1>
              <p>
                {project?.description ||
                  "创建项目并连接 GitHub / GitLab 仓库，开始第一条 AI 交付流水线。"}
              </p>
            </div>
            <div className="date-stamp">
              <b>05</b>
              <span>
                AUG
                <br />
                2026
              </span>
            </div>
          </div>

          <section className="metrics">
            <Metric
              label="活跃需求"
              value={active.length}
              detail={`${requirements.length} 条总需求`}
              tone="cyan"
            />
            <Metric
              label="关联仓库"
              value={repositories.length}
              detail="GitHub / GitLab"
              tone="violet"
            />
            <Metric
              label="等待人工确认"
              value={
                requirements.filter((item) =>
                  item.status.startsWith("awaiting_"),
                ).length
              }
              detail="需要负责人决策"
              tone="amber"
            />
            <Metric
              label="阻塞"
              value={blocked}
              detail={blocked ? "已触发人工介入" : "流程运行正常"}
              tone="red"
            />
          </section>

          <section className="main-grid">
            <div className="panel requirements-panel" id="requirements">
              <div className="panel-heading">
                <div>
                  <p className="eyebrow">DELIVERY PIPELINE</p>
                  <h2>需求运行中</h2>
                </div>
                <button className="text-button">查看全部 →</button>
              </div>
              <div className="requirement-list">
                {projectDataLoading ? (
                  <LoadingState label="正在加载当前项目的需求…" />
                ) : requirements.length === 0 ? (
                  <EmptyState
                    action={() => setShowCreate(true)}
                    disabled={!canPublishRequirement}
                    message={publishRequirementHint}
                  />
                ) : (
                  requirements
                    .slice(0, 5)
                    .map((item) => (
                      <RequirementRow
                        key={item.id}
                        item={item}
                        action={() => setSelectedRequirement(item)}
                      />
                    ))
                )}
              </div>
            </div>

            <div className="panel agents-panel" id="agents">
              <div className="panel-heading">
                <div>
                  <p className="eyebrow">AGENT CELL</p>
                  <h2>四人小队</h2>
                </div>
                <span className="live-label">
                  <i /> READY
                </span>
              </div>
              <div className="agent-stack">
                {agents.map(([number, name, description], index) => (
                  <article key={number}>
                    <span className={`agent-number a${index + 1}`}>
                      {number}
                    </span>
                    <div>
                      <h3>{name}</h3>
                      <p>{description}</p>
                    </div>
                    <span className="agent-ready">待命</span>
                  </article>
                ))}
              </div>
            </div>
          </section>

          <section className="panel repo-panel" id="repositories">
            <div className="panel-heading">
              <div>
                <p className="eyebrow">
                  SOURCE CONNECTIONS · {project?.name ?? "NO PROJECT"}
                </p>
                <h2>代码仓库</h2>
              </div>
              <button
                className="secondary-button"
                onClick={() => setShowRepository(true)}
                disabled={!project}
                title={
                  project ? `添加到 ${project.name}` : "请先创建或选择项目"
                }
              >
                ＋ 添加仓库
              </button>
            </div>
            <div className="repo-grid">
              {projectDataLoading ? (
                <p className="muted">正在加载当前项目的仓库…</p>
              ) : repositories.length === 0 ? (
                <p className="muted repo-empty-copy">
                  {project
                    ? `“${project.name}”还没有关联仓库。先添加仓库，之后才能发布需求。`
                    : "请先创建项目，再为项目添加 GitHub / GitLab 仓库。"}
                </p>
              ) : (
                repositories.map((repo) => (
                  <article key={repo.id}>
                    <span className={`provider ${repo.provider}`}>
                      {repo.provider === "github" ? "GH" : "GL"}
                    </span>
                    <div>
                      <h3>{repo.full_name}</h3>
                      <p>默认分支 · {repo.default_branch}</p>
                    </div>
                    <div className="repo-card-actions">
                      <span className="connected">
                        {repo.webhook_status === "active" ? "已验证" : "待验证"}
                      </span>
                      <button
                        className="text-button"
                        onClick={() => setEditingRepository(repo)}
                      >
                        编辑
                      </button>
                    </div>
                  </article>
                ))
              )}
            </div>
          </section>
        </div>
      </section>

      {showCreate && (
        <CreateRequirementModal
          project={project}
          repositories={repositories}
          close={() => setShowCreate(false)}
          onCreated={(item) => {
            setRequirements((current) => [item, ...current]);
            setShowCreate(false);
          }}
          api={api}
        />
      )}
      {showProject && (
        <CreateProjectModal
          close={() => setShowProject(false)}
          api={api}
          onCreated={(item) => {
            setProjects((current) => [item, ...current]);
            activateProject(item);
            setShowProject(false);
          }}
        />
      )}
      {(showRepository || editingRepository) && project && (
        <RepositoryModal
          project={project}
          repository={editingRepository}
          close={() => {
            setShowRepository(false);
            setEditingRepository(null);
          }}
          api={api}
          onSaved={(item) => {
            setRepositories((current) =>
              current.some((entry) => entry.id === item.id)
                ? current.map((entry) => (entry.id === item.id ? item : entry))
                : [...current, item],
            );
            setShowRepository(false);
            setEditingRepository(null);
          }}
        />
      )}
      {showSettings && user.system_role === "admin" && (
        <PlatformSettingsModal close={() => setShowSettings(false)} api={api} />
      )}
      {selectedRequirement && (
        <RequirementDrawer
          item={selectedRequirement}
          repositories={repositories}
          close={() => setSelectedRequirement(null)}
          api={api}
          transition={runTransition}
        />
      )}
      {error && (
        <button className="toast" onClick={() => setError("")}>
          {error}
        </button>
      )}
    </main>
  );
}

function Metric({
  label,
  value,
  detail,
  tone,
}: {
  label: string;
  value: number;
  detail: string;
  tone: string;
}) {
  return (
    <article className={`metric ${tone}`}>
      <p>{label}</p>
      <div>
        <strong>{String(value).padStart(2, "0")}</strong>
        <span>↗</span>
      </div>
      <small>{detail}</small>
    </article>
  );
}

function RequirementRow({
  item,
  action,
}: {
  item: Requirement;
  action: () => void;
}) {
  const step = [
    "draft",
    "clarifying",
    "awaiting_clarification",
    "planning",
    "awaiting_plan",
    "developing",
    "reviewing",
    "accepting",
    "awaiting_merge",
    "merging",
    "completed",
  ].indexOf(item.status);
  const progress = Math.max(4, Math.round(((step + 1) / 11) * 100));
  return (
    <article
      className="requirement-row"
      onClick={action}
      role="button"
      tabIndex={0}
    >
      <div className={`priority ${item.priority}`} />
      <div className="requirement-copy">
        <p>
          REQ-{String(item.number).padStart(3, "0")} ·{" "}
          {item.priority.toUpperCase()}
        </p>
        <h3>{item.title}</h3>
        <div className="progress">
          <span style={{ width: `${progress}%` }} />
        </div>
      </div>
      <div className="status-cell">
        <span className={`status ${item.status}`}>
          {statusLabel[item.status] ?? item.status}
        </span>
        <small>
          {progress}% · {item.status === "blocked" ? "查看原因" : "查看"} →
        </small>
      </div>
    </article>
  );
}

function EmptyState({
  action,
  disabled,
  message,
}: {
  action: () => void;
  disabled: boolean;
  message: string;
}) {
  return (
    <div className="empty-state">
      <span>◇</span>
      <h3>发布第一条需求</h3>
      <p>
        {disabled ? message : "需求澄清师会先把目标、边界和验收标准梳理清楚。"}
      </p>
      <button
        className="secondary-button"
        onClick={action}
        disabled={disabled}
        title={message}
      >
        开始创建
      </button>
    </div>
  );
}

function LoadingState({ label }: { label: string }) {
  return (
    <div className="loading-state">
      <span className="live-dot" />
      {label}
    </div>
  );
}

function CreateRequirementModal({
  project,
  repositories,
  close,
  onCreated,
  api,
}: {
  project: Project | null;
  repositories: Repository[];
  close: () => void;
  onCreated: (item: Requirement) => void;
  api: <T>(path: string, init?: RequestInit) => Promise<T>;
}) {
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const [attachments, setAttachments] = useState<PendingRequirementImage[]>([]);
  const [selectedRepositories, setSelectedRepositories] = useState<string[]>(
    repositories.map((repo) => repo.id),
  );
  async function pasteImages(event: ClipboardEvent<HTMLFormElement>) {
    const files = Array.from(event.clipboardData.items)
      .filter((item) => item.kind === "file" && item.type.startsWith("image/"))
      .map((item) => item.getAsFile())
      .filter((file): file is File => Boolean(file));
    if (files.length === 0) return;
    event.preventDefault();
    if (attachments.length + files.length > MAX_REQUIREMENT_IMAGES) {
      setMessage(`每条需求最多粘贴 ${MAX_REQUIREMENT_IMAGES} 张截图。`);
      return;
    }
    const nextTotal =
      attachments.reduce((total, item) => total + item.size_bytes, 0) +
      files.reduce((total, file) => total + file.size, 0);
    if (nextTotal > MAX_REQUIREMENT_IMAGES_TOTAL_BYTES) {
      setMessage("截图总大小不能超过 15 MB。");
      return;
    }
    try {
      const prepared = await Promise.all(
        files.map((file, index) => prepareRequirementImage(file, index)),
      );
      setAttachments((current) => [...current, ...prepared]);
      setMessage("");
    } catch (reason) {
      setMessage(reason instanceof Error ? reason.message : String(reason));
    }
  }
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!project || selectedRepositories.length === 0) {
      setMessage("请至少选择一个目标代码仓库。");
      return;
    }
    const form = new FormData(event.currentTarget);
    setBusy(true);
    try {
      const chosen = selectedRepositories
        .map((id) => repositories.find((repo) => repo.id === id))
        .filter((repo): repo is Repository => Boolean(repo));
      const item = await api<Requirement>(
        `/api/v1/projects/${project.id}/requirements`,
        {
          method: "POST",
          body: JSON.stringify({
            title: form.get("title"),
            description: form.get("description"),
            priority: form.get("priority"),
            repositories: chosen.map((repo, index) => ({
              repository_id: repo.id,
              target_branch: repo.default_branch,
              merge_order: index,
            })),
            attachments: attachments.map((attachment) => ({
              filename: attachment.filename,
              media_type: attachment.media_type,
              data_base64: attachment.data_base64,
            })),
          }),
        },
      );
      const published = await api<{ requirement: Requirement }>(
        `/api/v1/requirements/${item.id}/transitions`,
        {
          method: "POST",
          body: JSON.stringify({
            event: "publish",
            expected_version: item.version,
            reason: "从 Web 工作台发布",
          }),
        },
      );
      onCreated(published.requirement);
    } catch (reason) {
      setMessage(String(reason));
    } finally {
      setBusy(false);
    }
  }
  function toggleRepository(id: string) {
    setSelectedRepositories((current) =>
      current.includes(id)
        ? current.filter((item) => item !== id)
        : [...current, id],
    );
  }
  return (
    <div className="modal-backdrop" role="dialog" aria-modal="true">
      <form className="modal" onSubmit={submit} onPaste={pasteImages}>
        <div className="panel-heading">
          <div>
            <p className="eyebrow">NEW REQUIREMENT</p>
            <h2>发布需求</h2>
          </div>
          <button type="button" className="close-button" onClick={close}>
            ×
          </button>
        </div>
        {project && (
          <ProjectContext
            project={project}
            message="需求及其 Agent 流程将归属于当前项目"
          />
        )}
        <label>
          需求标题
          <input
            name="title"
            required
            minLength={3}
            placeholder="例如：支持项目成员邀请"
          />
        </label>
        <label>
          详细描述
          <textarea
            name="description"
            required
            minLength={10}
            rows={6}
            placeholder="说明目标、用户场景和预期结果。Agent 会继续澄清细节。"
          />
        </label>
        <section
          className="requirement-image-input"
          tabIndex={0}
          aria-label="粘贴需求截图"
        >
          <div className="requirement-image-paste-hint">
            <span aria-hidden="true">▣</span>
            <div>
              <strong>粘贴截图</strong>
              <p>在此表单内按 ⌘V 或 Ctrl+V，可粘贴 PNG、JPG、WebP（单张 5 MB）。</p>
            </div>
          </div>
          {attachments.length > 0 && (
            <div className="requirement-image-previews" aria-live="polite">
              {attachments.map((attachment) => (
                <figure key={attachment.id}>
                  <Image
                    src={attachment.preview_url}
                    alt={attachment.filename}
                    width={180}
                    height={112}
                    unoptimized
                  />
                  <figcaption>
                    <span>
                      {attachment.filename}
                      <small>
                        {(attachment.size_bytes / 1024 / 1024).toFixed(2)} MB
                      </small>
                    </span>
                    <button
                      type="button"
                      onClick={() =>
                        setAttachments((current) =>
                          current.filter((item) => item.id !== attachment.id),
                        )
                      }
                      disabled={busy}
                      aria-label={`移除 ${attachment.filename}`}
                    >
                      移除
                    </button>
                  </figcaption>
                </figure>
              ))}
            </div>
          )}
        </section>
        <label>
          优先级
          <select name="priority" defaultValue="medium">
            <option value="low">低</option>
            <option value="medium">中</option>
            <option value="high">高</option>
            <option value="critical">紧急</option>
          </select>
        </label>
        <div className="selected-repos">
          <p>目标仓库与合并顺序</p>
          {repositories.map((repo) => {
            const order = selectedRepositories.indexOf(repo.id);
            return (
              <label className="repo-choice" key={repo.id}>
                <input
                  type="checkbox"
                  checked={order >= 0}
                  onChange={() => toggleRepository(repo.id)}
                />
                <span>
                  {repo.full_name}
                  <small>
                    {order >= 0
                      ? `第 ${order + 1} 个合并 · ${repo.default_branch}`
                      : "不参与本需求"}
                  </small>
                </span>
              </label>
            );
          })}
        </div>
        {message && <p className="error-message">{message}</p>}
        <div className="modal-actions">
          <button type="button" className="ghost-button" onClick={close}>
            取消
          </button>
          <button className="primary-button compact" disabled={busy}>
            {busy ? "正在发布…" : "发布并开始澄清"}
          </button>
        </div>
      </form>
    </div>
  );
}

function CreateProjectModal({
  close,
  onCreated,
  api,
}: {
  close: () => void;
  onCreated: (item: Project) => void;
  api: <T>(path: string, init?: RequestInit) => Promise<T>;
}) {
  const [message, setMessage] = useState("");
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    try {
      onCreated(
        await api<Project>("/api/v1/projects", {
          method: "POST",
          body: JSON.stringify({
            key: String(form.get("key")).toUpperCase(),
            name: form.get("name"),
            description: form.get("description"),
          }),
        }),
      );
    } catch (reason) {
      setMessage(String(reason));
    }
  }
  return (
    <div className="modal-backdrop" role="dialog" aria-modal="true">
      <form className="modal small-modal" onSubmit={submit}>
        <div className="panel-heading">
          <div>
            <p className="eyebrow">NEW PROJECT</p>
            <h2>创建项目</h2>
          </div>
          <button type="button" className="close-button" onClick={close}>
            ×
          </button>
        </div>
        <label>
          项目代号
          <input
            name="key"
            required
            pattern="[A-Za-z][A-Za-z0-9_-]{1,39}"
            placeholder="PLATFORM"
          />
        </label>
        <label>
          项目名称
          <input name="name" required minLength={2} placeholder="研发平台" />
        </label>
        <label>
          项目说明
          <textarea name="description" rows={4} />
        </label>
        {message && <p className="error-message">{message}</p>}
        <div className="modal-actions">
          <button type="button" className="ghost-button" onClick={close}>
            取消
          </button>
          <button className="primary-button compact">创建项目</button>
        </div>
      </form>
    </div>
  );
}

function RepositoryModal({
  project,
  repository,
  close,
  onSaved,
  api,
}: {
  project: Project;
  repository: Repository | null;
  close: () => void;
  onSaved: (item: Repository) => void;
  api: <T>(path: string, init?: RequestInit) => Promise<T>;
}) {
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    const fullName = String(form.get("full_name"));
    setBusy(true);
    setMessage("");
    try {
      onSaved(
        await api<Repository>(
          repository
            ? `/api/v1/projects/${project.id}/repositories/${repository.id}`
            : `/api/v1/projects/${project.id}/repositories`,
          {
          method: repository ? "PUT" : "POST",
          body: JSON.stringify({
            provider: form.get("provider"),
            external_id: form.get("external_id") || fullName,
            full_name: fullName,
            clone_url: form.get("clone_url"),
            web_url: form.get("web_url"),
            default_branch: form.get("default_branch"),
          }),
          },
        ),
      );
    } catch (reason) {
      setMessage(String(reason));
    } finally {
      setBusy(false);
    }
  }
  return (
    <div className="modal-backdrop" role="dialog" aria-modal="true">
      <form className="modal" onSubmit={submit}>
        <div className="panel-heading">
          <div>
            <p className="eyebrow">SOURCE CONNECTION</p>
            <h2>{repository ? "编辑代码仓库" : "添加代码仓库"}</h2>
          </div>
          <button type="button" className="close-button" onClick={close}>
            ×
          </button>
        </div>
        <ProjectContext
          project={project}
          message={
            repository
              ? "修改只影响此项目的仓库连接；活跃需求期间不能改变仓库身份"
              : "保存后，此仓库只会出现在当前项目中"
          }
        />
        <label>
          Provider
          <select name="provider" defaultValue={repository?.provider ?? "github"}>
            <option value="github">GitHub</option>
            <option value="gitlab">GitLab</option>
          </select>
        </label>
        <label>
          仓库全名
          <input
            name="full_name"
            required
            defaultValue={repository?.full_name ?? ""}
            placeholder="organization/repository"
          />
        </label>
        <label>
          Provider 外部 ID
          <input
            name="external_id"
            defaultValue={repository?.external_id ?? ""}
            placeholder="GitHub 可填写仓库数字 ID；留空时使用全名"
          />
        </label>
        <label>
          Clone URL
          <input
            name="clone_url"
            type="text"
            required
            defaultValue={repository?.clone_url ?? ""}
            placeholder="git@github.com:organization/repository.git"
          />
          <small className="field-hint">
            推荐使用 SSH 地址，推拉代码由可信 Git Worker 完成。
          </small>
        </label>
        <label>
          Web URL
          <input
            name="web_url"
            type="url"
            required
            defaultValue={repository?.web_url ?? ""}
            placeholder="https://github.com/organization/repository"
          />
        </label>
        <label>
          默认分支
          <input
            name="default_branch"
            defaultValue={repository?.default_branch ?? "main"}
            required
          />
        </label>
        {message && <p className="error-message">{message}</p>}
        <div className="modal-actions">
          <button type="button" className="ghost-button" onClick={close}>
            取消
          </button>
          <button className="primary-button compact" disabled={busy}>
            {busy
              ? "正在保存…"
              : repository
                ? "保存仓库修改"
                : `添加到 ${project.name}`}
          </button>
        </div>
      </form>
    </div>
  );
}

function PlatformSettingsModal({
  close,
  api,
}: {
  close: () => void;
  api: <T>(path: string, init?: RequestInit) => Promise<T>;
}) {
  const [credentials, setCredentials] = useState<ProviderCredentialStatus[]>([]);
  const [busyProvider, setBusyProvider] = useState<string | null>(null);
  const [message, setMessage] = useState("");

  useEffect(() => {
    api<ProviderCredentialStatus[]>("/api/v1/admin/provider-credentials")
      .then(setCredentials)
      .catch((error) => setMessage(String(error)));
  }, [api]);

  async function saveCredential(
    provider: "github" | "gitlab",
    event: FormEvent<HTMLFormElement>,
  ) {
    event.preventDefault();
    const form = event.currentTarget;
    const token = String(new FormData(form).get("token") ?? "");
    setBusyProvider(provider);
    setMessage("");
    try {
      const updated = await api<ProviderCredentialStatus>(
        `/api/v1/admin/provider-credentials/${provider}`,
        { method: "PUT", body: JSON.stringify({ token }) },
      );
      setCredentials((current) => [
        ...current.filter((entry) => entry.provider !== provider),
        updated,
      ]);
      form.reset();
      setMessage(
        `${provider === "github" ? "GitHub" : "GitLab"} Token 已安全保存，Git Worker 会在下一次任务中使用。`,
      );
    } catch (error) {
      setMessage(`保存失败：${String(error)}`);
    } finally {
      setBusyProvider(null);
    }
  }

  async function removeCredential(provider: "github" | "gitlab") {
    setBusyProvider(provider);
    setMessage("");
    try {
      const updated = await api<ProviderCredentialStatus>(
        `/api/v1/admin/provider-credentials/${provider}`,
        { method: "DELETE" },
      );
      setCredentials((current) => [
        ...current.filter((entry) => entry.provider !== provider),
        updated,
      ]);
      setMessage(`${provider === "github" ? "GitHub" : "GitLab"} 页面托管 Token 已移除。`);
    } catch (error) {
      setMessage(`移除失败：${String(error)}`);
    } finally {
      setBusyProvider(null);
    }
  }

  return (
    <div className="modal-backdrop" role="dialog" aria-modal="true">
      <section className="modal settings-modal">
        <div className="panel-heading">
          <div>
            <p className="eyebrow">PLATFORM SETTINGS</p>
            <h2>Provider 凭据</h2>
          </div>
          <button type="button" className="close-button" onClick={close}>
            ×
          </button>
        </div>
        <p className="settings-intro">
          Token 只保存到受限 Secret Volume，并由 Git Worker 读取。页面、SQLite、日志和 Agent
          沙箱都不会获得或回显 Token 明文。
        </p>
        {(["github", "gitlab"] as const).map((provider) => {
          const status = credentials.find((entry) => entry.provider === provider);
          const label = provider === "github" ? "GitHub" : "GitLab";
          return (
            <form
              className="credential-card"
              key={provider}
              onSubmit={(event) => saveCredential(provider, event)}
            >
              <div className="credential-heading">
                <div>
                  <b>{label}</b>
                  <small>
                    {status?.configured
                      ? status.source === "managed"
                        ? "已通过页面配置"
                        : "已通过环境变量配置"
                      : "尚未配置"}
                  </small>
                </div>
                <span className={status?.configured ? "configured" : "missing"}>
                  {status?.configured ? "● 已配置" : "○ 未配置"}
                </span>
              </div>
              <label>
                {status?.configured ? "替换 Token" : "Token"}
                <input
                  name="token"
                  type="password"
                  autoComplete="new-password"
                  minLength={20}
                  required
                  placeholder="粘贴 Token；保存后不会再次显示"
                />
              </label>
              <div className="credential-actions">
                <button
                  className="primary-button compact"
                  disabled={busyProvider === provider}
                >
                  {busyProvider === provider ? "正在保存…" : "保存 Token"}
                </button>
                {status?.source === "managed" && (
                  <button
                    type="button"
                    className="secondary-button compact"
                    disabled={busyProvider === provider}
                    onClick={() => removeCredential(provider)}
                  >
                    移除页面托管 Token
                  </button>
                )}
              </div>
            </form>
          );
        })}
        {message && (
          <p className={message.includes("失败") ? "error-message" : "success-message"}>
            {message}
          </p>
        )}
      </section>
    </div>
  );
}

function ProjectContext({
  project,
  message,
}: {
  project: Project;
  message: string;
}) {
  return (
    <div className="project-context">
      <span className="project-symbol">{project.key.slice(0, 2)}</span>
      <div>
        <small>当前项目</small>
        <strong>{project.name}</strong>
        <p>{message}</p>
      </div>
    </div>
  );
}

function RequirementDrawer({
  item,
  repositories,
  close,
  api,
  transition,
}: {
  item: Requirement;
  repositories: Repository[];
  close: () => void;
  api: <T>(path: string, init?: RequestInit) => Promise<T>;
  transition: (
    item: Requirement,
    event: string,
    reason?: string,
  ) => Promise<void>;
}) {
  const [artifacts, setArtifacts] = useState<Artifact[]>([]);
  const [attachments, setAttachments] = useState<RequirementAttachment[]>([]);
  const [timeline, setTimeline] = useState<TimelineItem[]>([]);
  const [requirementRepositories, setRequirementRepositories] = useState<
    RequirementRepository[]
  >([]);
  const [agentRuns, setAgentRuns] = useState<AgentRun[]>([]);
  const [workflowTasks, setWorkflowTasks] = useState<WorkflowTask[]>([]);
  const [evidence, setEvidence] = useState<Evidence[]>([]);
  const [messages, setMessages] = useState<ConversationMessage[]>([]);
  const [providerCapabilities, setProviderCapabilities] =
    useState<ProviderCapabilities | null>(null);
  const [reason, setReason] = useState("");
  const [showCloseRequirement, setShowCloseRequirement] = useState(false);
  const [closeReason, setCloseReason] = useState("");
  const [pullRequestNumber, setPullRequestNumber] = useState("");
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);
  const autoAdvancedClarification = useRef<string | null>(null);
  const canCloseRequirement = ![
    "completed",
    "cancelled",
    "merging",
  ].includes(item.status);
  useEffect(() => {
    let cancelled = false;
    Promise.all([
      api<Artifact[]>(`/api/v1/requirements/${item.id}/artifacts`),
      api<RequirementAttachment[]>(
        `/api/v1/requirements/${item.id}/attachments`,
      ),
      api<TimelineItem[]>(`/api/v1/requirements/${item.id}/timeline`),
      api<RequirementRepository[]>(
        `/api/v1/requirements/${item.id}/repositories`,
      ),
      api<AgentRun[]>(`/api/v1/requirements/${item.id}/agent-runs`),
      api<WorkflowTask[]>(`/api/v1/requirements/${item.id}/tasks`),
      api<Evidence[]>(`/api/v1/requirements/${item.id}/evidence`),
      api<ConversationMessage[]>(`/api/v1/requirements/${item.id}/messages`),
      api<ProviderCapabilities>("/api/v1/provider-capabilities"),
    ])
      .then(
        ([
          artifactItems,
          attachmentItems,
          timelineItems,
          repositoryItems,
          runItems,
          taskItems,
          evidenceItems,
          conversationItems,
          capabilityItems,
        ]) => {
          if (cancelled) return;
          setArtifacts(artifactItems);
          setAttachments(attachmentItems);
          setTimeline(timelineItems);
          setRequirementRepositories(repositoryItems);
          setAgentRuns(runItems);
          setWorkflowTasks(taskItems);
          setEvidence(evidenceItems);
          setMessages(conversationItems);
          setProviderCapabilities(capabilityItems);
        },
      )
      .catch((error) => {
        if (!cancelled) setMessage(String(error));
      });
    return () => {
      cancelled = true;
    };
  }, [api, item.id, item.status]);
  const autoRefreshSwimlane = requirementIsRunning(item.status);
  useEffect(() => {
    if (!autoRefreshSwimlane) return;
    let cancelled = false;
    let timer: number | undefined;

    async function refreshSwimlane() {
      try {
        const [timelineItems, runItems, taskItems] = await Promise.all([
          api<TimelineItem[]>(`/api/v1/requirements/${item.id}/timeline`),
          api<AgentRun[]>(`/api/v1/requirements/${item.id}/agent-runs`),
          api<WorkflowTask[]>(`/api/v1/requirements/${item.id}/tasks`),
        ]);
        if (cancelled) return;
        setTimeline(timelineItems);
        setAgentRuns(runItems);
        setWorkflowTasks(taskItems);
      } catch {
        // A transient refresh failure must not replace data already on screen.
      } finally {
        if (!cancelled) {
          timer = window.setTimeout(
            refreshSwimlane,
            SWIMLANE_REFRESH_INTERVAL_MS,
          );
        }
      }
    }

    timer = window.setTimeout(
      refreshSwimlane,
      SWIMLANE_REFRESH_INTERVAL_MS,
    );
    return () => {
      cancelled = true;
      if (timer !== undefined) window.clearTimeout(timer);
    };
  }, [api, autoRefreshSwimlane, item.id]);
  const actions: Record<string, [string, string][]> = {
    draft: [["publish", "发布并澄清"]],
    awaiting_clarification: [["request_more_clarification", "要求补充"]],
    awaiting_plan: [
      ["confirm_plan", "确认方案并开发"],
      ["request_plan_change", "要求调整方案"],
    ],
    awaiting_merge: [["begin_merge", "确认合并下一仓"]],
    blocked: [
      ["retry_clarification", "重试需求澄清"],
      ["retry_development", "从开发重试"],
      ["retry_review", "重试代码评审"],
      ["retry_planning", "从方案重试"],
      ["retry_acceptance", "从验收重试"],
      ["retry_regression", "重新组合回归"],
      ["retry_merge", "重新准备合并"],
    ],
  };
  const nextMergeLink = requirementRepositories
    .slice()
    .sort((left, right) => left.merge_order - right.merge_order)
    .find((link) => link.status !== "merged");
  const nextMergeRepository = repositories.find(
    (candidate) => candidate.id === nextMergeLink?.repository_id,
  );
  const needsPullRequestRegistration =
    item.status === "awaiting_merge" &&
    Boolean(nextMergeLink?.head_sha) &&
    !nextMergeLink?.pull_request_number;
  const providerApiEnabled = nextMergeRepository
    ? nextMergeRepository.provider === "gitlab"
      ? providerCapabilities?.gitlab_api_enabled === true
      : providerCapabilities?.github_api_enabled === true
    : false;
  const needsProviderToken =
    item.status === "awaiting_merge" &&
    Boolean(nextMergeLink?.head_sha && nextMergeLink.pull_request_number) &&
    providerCapabilities !== null &&
    !providerApiEnabled;
  const canBeginMerge =
    item.status !== "awaiting_merge" ||
    Boolean(
      nextMergeLink?.head_sha &&
        nextMergeLink.pull_request_number &&
        providerApiEnabled,
    );
  const clarificationArtifact = artifacts
    .slice()
    .reverse()
    .find((artifact) => artifact.kind === "clarification_spec");
  const architectureArtifact = artifacts
    .slice()
    .reverse()
    .find((artifact) => artifact.kind === "architecture_plan");
  const architecturePlan = architectureArtifact?.content as
    | Partial<ArchitecturePlanContent>
    | undefined;
  const clarificationSummary =
    typeof clarificationArtifact?.content.summary === "string"
      ? clarificationArtifact.content.summary
      : "需求澄清师正在整理需求信息。";
  const openQuestions = Array.isArray(
    clarificationArtifact?.content.open_questions,
  )
    ? clarificationArtifact.content.open_questions.filter(
        (question): question is string =>
          typeof question === "string" && question.trim().length > 0,
      )
    : [];
  const isClarificationStage = [
    "clarifying",
    "awaiting_clarification",
  ].includes(item.status);
  const latestFailedRun = agentRuns
    .slice()
    .reverse()
    .find((run) => run.status === "failed");
  const latestBlockedTransition = timeline
    .slice()
    .reverse()
    .find((entry) => entry.to_status === "blocked");
  const blockedDiagnostic =
    item.status === "blocked"
      ? failureDiagnostic(latestFailedRun, latestBlockedTransition)
      : null;
  useEffect(() => {
    if (
      item.status !== "awaiting_clarification" ||
      !clarificationArtifact ||
      openQuestions.length > 0 ||
      autoAdvancedClarification.current === clarificationArtifact.id
    )
      return;
    autoAdvancedClarification.current = clarificationArtifact.id;
    transition(
      item,
      "confirm_clarification",
      "澄清问题已全部解决，系统自动进入方案设计",
    ).catch((error) => setMessage(String(error)));
  }, [clarificationArtifact, item, openQuestions.length, transition]);
  async function act(event: string) {
    if (event === "request_more_clarification" && !reason.trim()) {
      setMessage("请先填写对需求澄清师问题的回答或需要补充的说明。");
      return;
    }
    if (event === "request_plan_change" && !reason.trim()) {
      setMessage("请先填写需要系统架构师调整的具体内容。");
      return;
    }
    setBusy(true);
    setMessage("");
    try {
      await transition(item, event, reason);
      setReason("");
    } catch (error) {
      setMessage(String(error));
    } finally {
      setBusy(false);
    }
  }
  async function cancelRequirement() {
    if (!closeReason.trim()) {
      setMessage("请填写关闭原因，便于后续审计和追溯。");
      return;
    }
    setBusy(true);
    setMessage("");
    try {
      await transition(item, "cancel", closeReason.trim());
      setShowCloseRequirement(false);
      setCloseReason("");
      setMessage("需求已关闭，后续排队任务已终止；历史记录和已有交付物仍会保留。");
    } catch (error) {
      setMessage(String(error));
    } finally {
      setBusy(false);
    }
  }
  async function comment(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!reason.trim()) return;
    try {
      const created = await api<ConversationMessage>(
        `/api/v1/requirements/${item.id}/messages`,
        { method: "POST", body: JSON.stringify({ body: reason }) },
      );
      setMessages((current) => [...current, created]);
      setReason("");
      setMessage("讨论消息已记录到审计链路，并会进入下一轮 Agent 上下文。");
    } catch (error) {
      setMessage(String(error));
    }
  }
  async function registerPullRequest() {
    const number = Number(pullRequestNumber);
    if (
      !nextMergeLink?.work_branch ||
      !nextMergeLink.head_sha ||
      !nextMergeRepository ||
      !Number.isInteger(number) ||
      number < 1
    ) {
      setMessage("请输入有效的 PR / Merge Request 编号。");
      return;
    }
    setBusy(true);
    setMessage("");
    try {
      const updated = await api<RequirementRepository>(
        `/api/v1/requirements/${item.id}/repositories/${nextMergeLink.id}`,
        {
          method: "PATCH",
          body: JSON.stringify({
            work_branch: nextMergeLink.work_branch,
            pull_request_number: number,
            pull_request_url: null,
            head_sha: nextMergeLink.head_sha,
          }),
        },
      );
      setRequirementRepositories((current) =>
        current.map((entry) => (entry.id === updated.id ? updated : entry)),
      );
      setPullRequestNumber("");
      setMessage(`PR #${number} 已登记。`);
    } catch (error) {
      setMessage(`登记 PR 失败：${String(error)}`);
    } finally {
      setBusy(false);
    }
  }
  async function createPullRequest() {
    if (!nextMergeLink || !nextMergeRepository || !providerApiEnabled) {
      setMessage("Provider API 尚未启用，请先配置 Token 并重启服务。");
      return;
    }
    setBusy(true);
    setMessage("画板正在通过 Git Worker 创建 PR…");
    try {
      const queued = await api<{ task_id: string; status: string }>(
        `/api/v1/requirements/${item.id}/repositories/${nextMergeLink.id}/pull-request`,
        { method: "POST" },
      );
      for (let attempt = 0; attempt < 40; attempt += 1) {
        await new Promise((resolve) => window.setTimeout(resolve, 750));
        const [links, task] = await Promise.all([
          api<RequirementRepository[]>(
            `/api/v1/requirements/${item.id}/repositories`,
          ),
          api<WorkflowTaskState>(
            `/api/v1/requirements/${item.id}/tasks/${queued.task_id}`,
          ),
        ]);
        setRequirementRepositories(links);
        const created = links.find((entry) => entry.id === nextMergeLink.id);
        if (created?.pull_request_number) {
          setMessage(
            `PR #${created.pull_request_number} 已由画板创建，可以继续确认合并。`,
          );
          return;
        }
        if (task.status === "failed") {
          const diagnostic = [task.error_code, task.error_message]
            .filter(Boolean)
            .join(": ");
          throw new Error(diagnostic || "Git Worker 创建 PR 失败");
        }
      }
      throw new Error("创建 PR 等待超时，可重试或使用下方手工登记方式");
    } catch (error) {
      setMessage(`自动创建 PR 失败：${String(error)}`);
    } finally {
      setBusy(false);
    }
  }
  return (
    <aside className="detail-drawer">
      <div className="drawer-head">
        <div>
          <p className="eyebrow">REQ-{String(item.number).padStart(3, "0")}</p>
          <h2>{item.title}</h2>
        </div>
        <div className="drawer-head-actions">
          {canCloseRequirement && (
            <button
              className="danger-button compact"
              onClick={() => setShowCloseRequirement(true)}
            >
              关闭需求
            </button>
          )}
          <button className="close-button" onClick={close} aria-label="关闭详情">
            ×
          </button>
        </div>
      </div>
      <div className="drawer-body">
        <span className={`status ${item.status}`}>
          {statusLabel[item.status] ?? item.status}
        </span>
        <p className="requirement-description">{item.description}</p>
        {showCloseRequirement && canCloseRequirement && (
          <section className="close-requirement-panel" role="alertdialog">
            <div>
              <p className="eyebrow">CLOSE REQUIREMENT</p>
              <h3>确认关闭这个需求？</h3>
              <p>
                关闭后将停止后续 Agent、依赖准备和自动化任务。历史记录、附件与已有代码会保留，已经提交或合并的代码不会自动回滚。
              </p>
            </div>
            <label>
              关闭原因（必填）
              <textarea
                value={closeReason}
                onChange={(event) => setCloseReason(event.target.value)}
                rows={3}
                autoFocus
                placeholder="例如：需求不再需要，停止继续投入"
              />
            </label>
            <div className="decision-actions">
              <button
                className="danger-button compact"
                disabled={busy || !closeReason.trim()}
                onClick={cancelRequirement}
              >
                {busy ? "正在关闭…" : "确认关闭需求"}
              </button>
              <button
                className="secondary-button compact"
                disabled={busy}
                onClick={() => {
                  setShowCloseRequirement(false);
                  setCloseReason("");
                }}
              >
                继续处理
              </button>
            </div>
          </section>
        )}
        {attachments.length > 0 && (
          <section className="requirement-attachments">
            <div>
              <p className="eyebrow">REFERENCE IMAGES</p>
              <h3>需求截图</h3>
            </div>
            <div className="requirement-attachment-grid">
              {attachments.map((attachment) => (
                <a
                  key={attachment.id}
                  href={`${API}/api/v1/requirement-attachments/${attachment.id}/content`}
                  target="_blank"
                  rel="noreferrer"
                  title={`查看 ${attachment.filename}`}
                >
                  <Image
                    src={`${API}/api/v1/requirement-attachments/${attachment.id}/content`}
                    alt={attachment.filename}
                    width={240}
                    height={150}
                    unoptimized
                  />
                  <span>
                    {attachment.filename}
                    <small>
                      {(attachment.size_bytes / 1024 / 1024).toFixed(2)} MB
                    </small>
                  </span>
                </a>
              ))}
            </div>
          </section>
        )}
        {item.status === "cancelled" ? (
          <section className="closed-requirement-notice">
            <p className="eyebrow">REQUIREMENT CLOSED</p>
            <h3>这个需求已关闭</h3>
            <p>不会再调度新的工作；下方仍可查看完整协作记录、附件和已有交付物。</p>
            {message && <p className="success-message">{message}</p>}
          </section>
        ) : isClarificationStage ? (
          <section className="clarification-dialog">
            <div className="clarification-heading">
              <div>
                <p className="eyebrow">CLARIFICATION DIALOGUE</p>
                <h3>与需求澄清师对话</h3>
              </div>
              <span>需求澄清师</span>
            </div>
            <article className="agent-question-card">
              <div className="conversation-author">
                <span className="agent-number a1">01</span>
                <div>
                  <b>需求澄清师</b>
                  <small>
                    {clarificationArtifact
                      ? `规格 v${clarificationArtifact.version}`
                      : "正在分析"}
                  </small>
                </div>
              </div>
              <p>{clarificationSummary}</p>
              {openQuestions.length > 0 ? (
                <div className="open-questions">
                  <strong>需要你回答的问题</strong>
                  <ol>
                    {openQuestions.map((question, index) => (
                      <li key={`${index}-${question}`}>
                        <span>{String(index + 1).padStart(2, "0")}</span>
                        <p>{question}</p>
                      </li>
                    ))}
                  </ol>
                </div>
              ) : (
                clarificationArtifact && (
                  <p className="questions-resolved">
                    ✓ 当前没有未解决问题，正在自动进入方案设计。
                  </p>
                )
              )}
            </article>
            {messages.length > 0 && (
              <div className="conversation-history">
                {messages.map((entry) => (
                  <article key={entry.id}>
                    <div>
                      <b>需求提出者</b>
                      <small>
                        {parseApiTimestamp(entry.created_at).toLocaleString("zh-CN")}
                      </small>
                    </div>
                    <p>{entry.body}</p>
                  </article>
                ))}
              </div>
            )}
            {item.status === "clarifying" || openQuestions.length === 0 ? (
              <div className="clarification-running">
                <span className="live-dot" />
                {openQuestions.length === 0 && clarificationArtifact
                  ? "澄清已完成，正在启动系统架构师…"
                  : "需求澄清师正在根据你的回答更新需求规格…"}
              </div>
            ) : (
              <div className="clarification-reply">
                <label>
                  你的回答或补充说明
                  <textarea
                    value={reason}
                    onChange={(event) => setReason(event.target.value)}
                    rows={5}
                    placeholder="请按问题顺序回答；不确定的内容也可以说明需要需求澄清师给出建议。"
                  />
                </label>
                <div className="decision-actions">
                  <button
                    className="primary-button compact"
                    disabled={busy || !reason.trim()}
                    onClick={() => act("request_more_clarification")}
                  >
                    {busy ? "正在发送…" : "发送回答给需求澄清师"}
                  </button>
                </div>
                <form onSubmit={comment}>
                  <button className="text-button" disabled={!reason.trim()}>
                    仅记录讨论 →
                  </button>
                </form>
              </div>
            )}
            {message && (
              <p
                className={
                  message.includes("已记录")
                    ? "success-message"
                    : "error-message"
                }
              >
                {message}
              </p>
            )}
          </section>
        ) : item.status === "awaiting_plan" &&
          architectureArtifact &&
          architecturePlan ? (
          <ArchitectureReview
            artifact={architectureArtifact}
            plan={architecturePlan}
            repositories={repositories}
            messages={messages.filter(
              (entry) => entry.stage === "awaiting_plan",
            )}
            reason={reason}
            setReason={setReason}
            busy={busy}
            message={message}
            approve={() => act("confirm_plan")}
            requestChange={() => act("request_plan_change")}
            recordComment={comment}
          />
        ) : (
          <section className="decision-box">
            <p className="eyebrow">人工决策</p>
            {blockedDiagnostic && (
              <div className="failure-diagnostic" role="alert">
                <div className="failure-diagnostic-heading">
                  <span>!</span>
                  <div>
                    <small>阻塞原因</small>
                    <h3>{blockedDiagnostic.title}</h3>
                  </div>
                </div>
                <p>{blockedDiagnostic.summary}</p>
                <details open>
                  <summary>完整错误详情</summary>
                  <code>{blockedDiagnostic.detail}</code>
                </details>
                <strong>{blockedDiagnostic.recoveryLabel}</strong>
              </div>
            )}
            {needsPullRequestRegistration && nextMergeLink && (
              <div className="pull-request-gate">
                <p className="eyebrow">PULL REQUEST GATE</p>
                <h3>
                  {providerApiEnabled
                    ? "分支已推送，由画板创建 PR"
                    : `分支已推送，启用${
                        nextMergeRepository?.provider === "gitlab"
                          ? " GitLab"
                          : " GitHub"
                      } 自动创建 PR`}
                </h3>
                <p>
                  已评审提交{" "}
                  <code>{nextMergeLink.head_sha?.slice(0, 12)}</code>{" "}
                  位于 <code>{nextMergeLink.work_branch}</code>。
                  {providerApiEnabled ? (
                    <>Provider API 已启用，画板会让 Git Worker 创建或复用同分支 PR。</>
                  ) : (
                    <>
                      请先在 <code>.env.local</code> 配置
                      <code>
                        {nextMergeRepository?.provider === "gitlab"
                          ? "GITLAB_TOKEN"
                          : "GITHUB_TOKEN"}
                      </code>
                      并重启 control-plane 与 git-worker；Token 不会进入 Agent 容器。
                    </>
                  )}
                </p>
                {providerApiEnabled && (
                  <button
                    className="primary-button compact"
                    disabled={busy}
                    onClick={createPullRequest}
                  >
                    {busy ? "正在创建 PR…" : "由画板创建 PR"}
                  </button>
                )}
                <div className="manual-pr-fallback">
                  <strong>手工方式（备用）</strong>
                {nextMergeLink.pull_request_url && (
                  <a
                    className="secondary-button compact"
                    href={nextMergeLink.pull_request_url}
                    target="_blank"
                    rel="noreferrer"
                  >
                    打开 {nextMergeRepository?.provider === "gitlab" ? "GitLab" : "GitHub"} 创建 PR ↗
                  </a>
                )}
                <div className="pull-request-register">
                  <label>
                    创建完成后填写 PR / MR 编号
                    <input
                      inputMode="numeric"
                      min="1"
                      type="number"
                      value={pullRequestNumber}
                      onChange={(event) => setPullRequestNumber(event.target.value)}
                      placeholder="例如 42"
                    />
                  </label>
                  <button
                    className="primary-button compact"
                    disabled={busy || !pullRequestNumber}
                    onClick={registerPullRequest}
                  >
                    登记 PR
                  </button>
                </div>
                <small>登记时会校验仓库、编号、评审分支和 head SHA。</small>
                </div>
              </div>
            )}
            {needsProviderToken && nextMergeLink && (
              <div className="pull-request-gate provider-token-gate">
                <p className="eyebrow">PROVIDER AUTHORIZATION</p>
                <h3>PR 已登记，自动合并凭据尚未配置</h3>
                <p>
                  画板不会在缺少 Provider 授权时尝试合并。请在环境变量中配置
                  <code>
                    {nextMergeRepository?.provider === "gitlab"
                      ? "GITLAB_TOKEN"
                      : "GITHUB_TOKEN"}
                  </code>
                  并重启服务，之后这里会显示“确认合并下一仓”。
                </p>
                {nextMergeLink.pull_request_url && (
                  <a
                    className="secondary-button compact"
                    href={nextMergeLink.pull_request_url}
                    target="_blank"
                    rel="noreferrer"
                  >
                    查看已登记的 PR / MR ↗
                  </a>
                )}
              </div>
            )}
            <label className="decision-note">
              补充说明（可选）
              <textarea
                value={reason}
                onChange={(event) => setReason(event.target.value)}
                rows={3}
                placeholder="记录重试原因、约束或需要 Agent 特别注意的内容"
              />
            </label>
            <div className="decision-actions">
              {(actions[item.status] ?? [])
                .filter(
                  ([event]) =>
                    (event !== "begin_merge" || canBeginMerge) &&
                    (event !== "retry_clarification" ||
                      latestFailedRun?.role === "clarify" ||
                      blockedDiagnostic?.recoveryEvent ===
                        "retry_clarification"),
                )
                .map(([event, label]) => (
                <button
                  key={event}
                  className={
                    event.startsWith("confirm") ||
                    event === "begin_merge" ||
                    event === "publish" ||
                    blockedDiagnostic?.recoveryEvent === event
                      ? "primary-button compact"
                      : "secondary-button compact"
                  }
                  disabled={busy}
                  onClick={() => act(event)}
                >
                  {label}
                </button>
                ))}
            </div>
            <form onSubmit={comment}>
              <button className="text-button" disabled={!reason.trim()}>
                仅记录讨论 →
              </button>
            </form>
            {message && (
              <p
                className={
                  message.includes("已记录")
                    ? "success-message"
                    : "error-message"
                }
              >
                {message}
              </p>
            )}
          </section>
        )}
        <section>
          <div className="swimlane-section-heading">
            <div>
              <p className="eyebrow section-label">COLLABORATION SWIMLANE</p>
              <h3>角色协作与任务交接</h3>
            </div>
            <p>
              时间从上向下推进；箭头表示任务、上下文或交付物的传递。
              {autoRefreshSwimlane && " 运行期间每 1.5 秒自动刷新。"}
            </p>
          </div>
          <WorkflowSwimlane
            agentRuns={agentRuns}
            timeline={timeline}
            workflowTasks={workflowTasks}
          />
        </section>
        <section>
          <p className="eyebrow section-label">DELIVERY REPOSITORIES</p>
          <div className="delivery-repositories">
            {requirementRepositories.map((link) => {
              const repo = repositories.find(
                (candidate) => candidate.id === link.repository_id,
              );
              return (
                <article key={link.id}>
                  <b>{link.merge_order + 1}</b>
                  <div>
                    <h3>{repo?.full_name ?? link.repository_id}</h3>
                    <p>
                      {link.work_branch ?? link.target_branch} ·{" "}
                      {link.head_sha?.slice(0, 8) ?? "等待交付"}
                    </p>
                  </div>
                  {link.pull_request_url ? (
                    <a
                      href={link.pull_request_url}
                      target="_blank"
                      rel="noreferrer"
                    >
                      {link.pull_request_number
                        ? "PR #" + link.pull_request_number + " ↗"
                        : "创建 PR ↗"}
                    </a>
                  ) : (
                    <span>
                      {repositoryStatusLabel[link.status] ?? link.status}
                    </span>
                  )}
                </article>
              );
            })}
          </div>
        </section>
        {evidence.length > 0 && (
          <section>
            <p className="eyebrow section-label">IMMUTABLE EVIDENCE</p>
            <div className="evidence-list">
              {evidence.map((entry) => (
                <a
                  key={entry.id}
                  href={`${API}/api/v1/evidence/${entry.id}/download`}
                >
                  <span>
                    {entry.kind.replaceAll("_", " ")}
                    <small>
                      {entry.sha256.slice(0, 16)}… ·{" "}
                      {Math.ceil(entry.size_bytes / 1024)} KiB
                    </small>
                  </span>
                  <b>下载 ↗</b>
                </a>
              ))}
            </div>
          </section>
        )}
        <section>
          <p className="eyebrow section-label">VERSIONED ARTIFACTS</p>
          {artifacts.length === 0 ? (
            <p className="muted">Agent 产物生成后会显示在这里。</p>
          ) : (
            artifacts
              .slice()
              .reverse()
              .map((artifact) => (
                <details className="artifact-card" key={artifact.id}>
                  <summary>
                    <span>{artifact.kind.replaceAll("_", " ")}</span>
                    <b>v{artifact.version}</b>
                  </summary>
                  <pre>{artifact.markdown}</pre>
                </details>
              ))
          )}
        </section>
        <section>
          <details className="audit-log">
            <summary>
              <span>
                <b>完整审计日志</b>
                <small>{timeline.length} 条状态变化与原始原因</small>
              </span>
              <strong>展开查看</strong>
            </summary>
            <div className="timeline">
              {timeline
                .slice()
                .reverse()
                .map((entry) => (
                  <article key={entry.id}>
                    <i />
                    <div>
                      <b>{entry.event}</b>
                      <p>
                        {entry.from_status} → {entry.to_status}
                      </p>
                      {entry.reason && (
                        <p className="timeline-reason">{entry.reason}</p>
                      )}
                      <small>
                        {entry.actor_type} ·{" "}
                        {parseApiTimestamp(entry.created_at).toLocaleString("zh-CN")}
                      </small>
                    </div>
                  </article>
                ))}
            </div>
          </details>
        </section>
      </div>
    </aside>
  );
}

function ArchitectureReview({
  artifact,
  plan,
  repositories,
  messages,
  reason,
  setReason,
  busy,
  message,
  approve,
  requestChange,
  recordComment,
}: {
  artifact: Artifact;
  plan: Partial<ArchitecturePlanContent>;
  repositories: Repository[];
  messages: ConversationMessage[];
  reason: string;
  setReason: (value: string) => void;
  busy: boolean;
  message: string;
  approve: () => void;
  requestChange: () => void;
  recordComment: (event: FormEvent<HTMLFormElement>) => Promise<void>;
}) {
  const repositoryPlans = Array.isArray(plan.repositories)
    ? plan.repositories
    : [];
  return (
    <section className="architecture-review">
      <div className="architecture-heading">
        <div>
          <p className="eyebrow">ARCHITECTURE REVIEW</p>
          <h3>实现方案评审</h3>
        </div>
        <span>
          系统架构师 · v{artifact.version} ·{" "}
          {typeof plan.confidence === "number"
            ? `方案置信度 ${plan.confidence}%`
            : "方案置信度未提供"}{" "}
          · 需人工审核
        </span>
      </div>
      <div className="architecture-summary">
        <div>
          <small>当前状态</small>
          <p>{plan.current_state || "未提供当前状态分析"}</p>
        </div>
        <div className="target-architecture">
          <small>目标方案</small>
          <p>{plan.target_architecture || "未提供目标方案"}</p>
        </div>
      </div>
      <PlanList title="执行步骤" items={plan.data_flow} numbered />
      <div className="repository-plan-list">
        <div className="plan-section-title">
          <span>02</span>
          <h4>仓库改动</h4>
        </div>
        {repositoryPlans.length === 0 ? (
          <p className="muted">方案未声明仓库改动。</p>
        ) : (
          repositoryPlans.map((repositoryPlan) => {
            const repository = repositories.find(
              (entry) => entry.id === repositoryPlan.repository_id,
            );
            return (
              <article key={repositoryPlan.repository_id}>
                <div className="repository-plan-head">
                  <div>
                    <b>
                      {repository?.full_name ?? repositoryPlan.repository_id}
                    </b>
                    <p>{repositoryPlan.purpose}</p>
                  </div>
                  <span>顺序 {repositoryPlan.merge_order + 1}</span>
                </div>
                <PlanItems label="计划改动" items={repositoryPlan.changes} />
                <PlanItems
                  label="验证命令"
                  items={repositoryPlan.test_commands}
                  code
                />
              </article>
            );
          })
        )}
      </div>
      <div className="architecture-grid">
        <PlanList title="接口影响" items={plan.public_interface_changes} />
        <PlanList
          title="数据库变更"
          items={plan.database_changes}
          empty="无数据库变更"
        />
        <PlanList title="安全考虑" items={plan.security_considerations} />
        <PlanList title="测试策略" items={plan.test_strategy} />
        <PlanList title="回滚方案" items={plan.migration_and_rollback} />
        <PlanList title="主要风险" items={plan.risks} tone="risk" />
      </div>
      {messages.length > 0 && (
        <div className="plan-feedback-history">
          <p className="eyebrow">历史调整意见</p>
          {messages.map((entry) => (
            <article key={entry.id}>
              <div>
                <b>项目负责人</b>
                <small>
                  {parseApiTimestamp(entry.created_at).toLocaleString("zh-CN")}
                </small>
              </div>
              <p>{entry.body}</p>
            </article>
          ))}
        </div>
      )}
      <div className="plan-approval">
        <p className="eyebrow">开工授权</p>
        <h4>本方案未达到自动批准条件，需要人工确认后再开始开发</h4>
        <p>如需退回，请写明需要修改的范围、约束或风险处理方式。</p>
        <label>
          审批意见
          <textarea
            value={reason}
            onChange={(event) => setReason(event.target.value)}
            rows={4}
            placeholder="批准时可留空；要求调整时必须填写具体意见。"
          />
        </label>
        <div className="decision-actions">
          <button
            className="primary-button compact"
            disabled={busy}
            onClick={approve}
          >
            {busy ? "正在处理…" : "批准方案并开始开发"}
          </button>
          <button
            className="secondary-button compact"
            disabled={busy || !reason.trim()}
            onClick={requestChange}
          >
            要求系统架构师调整
          </button>
        </div>
        <form onSubmit={recordComment}>
          <button className="text-button" disabled={!reason.trim()}>
            仅记录讨论 →
          </button>
        </form>
        {message && (
          <p
            className={
              message.includes("已记录") ? "success-message" : "error-message"
            }
          >
            {message}
          </p>
        )}
      </div>
    </section>
  );
}

function PlanList({
  title,
  items,
  empty = "暂无",
  numbered = false,
  tone = "",
}: {
  title: string;
  items: string[] | undefined;
  empty?: string;
  numbered?: boolean;
  tone?: string;
}) {
  const values = Array.isArray(items) ? items : [];
  return (
    <section className={`plan-list ${tone}`}>
      <div className="plan-section-title">
        <span>◇</span>
        <h4>{title}</h4>
      </div>
      {values.length === 0 ? (
        <p className="muted">{empty}</p>
      ) : (
        <ol className={numbered ? "numbered" : ""}>
          {values.map((value, index) => (
            <li key={`${index}-${value}`}>
              {numbered && <b>{String(index + 1).padStart(2, "0")}</b>}
              <span>{value}</span>
            </li>
          ))}
        </ol>
      )}
    </section>
  );
}

function PlanItems({
  label,
  items,
  code = false,
}: {
  label: string;
  items: string[];
  code?: boolean;
}) {
  return (
    <div className="plan-items">
      <strong>{label}</strong>
      <ul>
        {items.map((value, index) => (
          <li key={`${index}-${value}`} className={code ? "code" : ""}>
            {value}
          </li>
        ))}
      </ul>
    </div>
  );
}
