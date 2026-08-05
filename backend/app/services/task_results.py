import json
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..core.config import get_settings
from ..models.entities import AgentRun, ArtifactVersion, Evidence, MergeAttempt, OutboxEvent, Requirement, RequirementRepository, WorkflowTask
from .artifacts import ArtifactStore
from .workflow import transition_requirement


TASK_EVENTS = {
    "agent.clarify": "clarification_ready",
    "agent.architect": "plan_ready",
    "agent.develop": "development_ready",
    "agent.revise": "revision_ready",
    "agent.final_accept": "final_acceptance_passed",
    "git.prepare_workspaces": "workspace_ready",
    "git.prepare_analysis": "analysis_ready",
    "git.publish_changes": "changes_published",
    "git.prepare_verification": "verification_ready",
    "git.prepare_final_verification": "final_verification_ready",
    "git.prepare_incremental_verification": "incremental_verification_ready",
}

ARTIFACT_KINDS = {
    "agent.clarify": "clarification_spec",
    "agent.architect": "architecture_plan",
    "agent.develop": "development_report",
    "agent.review": "code_review_report",
    "agent.accept": "acceptance_report",
    "agent.revise": "architecture_revision",
    "agent.final_accept": "final_acceptance_report",
    "git.prepare_workspaces": "workspace_manifest",
    "git.prepare_analysis": "repository_analysis",
    "git.publish_changes": "delivery_manifest",
    "git.prepare_verification": "verification_manifest",
    "git.prepare_final_verification": "final_verification_manifest",
    "git.prepare_incremental_verification": "incremental_verification_manifest",
    "agent.regression": "incremental_regression_report",
}


def process_task_result(session: Session, result: dict[str, Any]) -> None:
    task = session.get(WorkflowTask, result["task_id"])
    if task is None or task.status in {"completed", "failed"}:
        return
    run = session.get(AgentRun, task.agent_run_id) if task.agent_run_id else None
    if result.get("status") != "completed":
        task.status = "failed"
        task.attempt_count += 1
        if run:
            run.status = "failed"
            run.error_code = result.get("error_code", "worker.failed")
            run.completed_at = datetime.now(UTC)
        if result.get("retryable") and task.attempt_count < get_settings().task_max_attempts:
            _schedule_retry(session, task, run)
            return
        if task.task_type == "git.merge_next":
            payload = json.loads(task.payload_json)
            context = payload.get("context", {})
            attempt = session.get(MergeAttempt, context.get("merge_attempt_id"))
            if attempt:
                attempt.status = "failed"
                attempt.error_code = result.get("error_code", "git.failed")
                attempt.completed_at = datetime.now(UTC)
            requirement = session.get(Requirement, task.requirement_id)
            if requirement:
                transition_requirement(session, requirement, "merge_failed", requirement.version, "worker", None, str(result.get("error_code", "git.failed")))
        elif task.task_type in {"git.prepare_workspaces", "git.publish_changes"}:
            requirement = session.get(Requirement, task.requirement_id)
            if requirement:
                transition_requirement(session, requirement, "automation_failed", requirement.version, "git_worker", None, str(result.get("error_code", "git.failed")))
        elif task.task_type in {
            "git.prepare_analysis",
            "git.prepare_verification",
            "git.prepare_final_verification",
            "git.prepare_incremental_verification",
        }:
            requirement = session.get(Requirement, task.requirement_id)
            if requirement:
                transition_requirement(session, requirement, "technical_failure", requirement.version, "git_worker", None, str(result.get("error_code", "git.failed")))
        elif task.task_type.startswith("agent."):
            requirement = session.get(Requirement, task.requirement_id)
            if requirement:
                transition_requirement(session, requirement, "technical_failure", requirement.version, "worker", None, str(result.get("error_code", "agent.failed")))
        return

    output = dict(result.get("output") or {})
    _externalize_large_evidence(session, task, output)
    task.status = "completed"
    if run:
        run.status = "completed"
        run.output_json = json.dumps(output, ensure_ascii=False)
        run.token_usage = int(result.get("token_usage", 0))
        run.completed_at = datetime.now(UTC)

    if task.task_type == "git.merge_next":
        payload = json.loads(task.payload_json)
        context = payload.get("context", {})
        target = session.get(RequirementRepository, context.get("requirement_repository_id"))
        attempt = session.get(MergeAttempt, context.get("merge_attempt_id"))
        if target:
            target.status = "merged"
            target.head_sha = str(output.get("merged_sha") or target.head_sha)
        if attempt:
            attempt.status = "completed"
            attempt.merged_sha = str(output.get("merged_sha", "")) or None
            attempt.completed_at = datetime.now(UTC)
        requirement = session.get(Requirement, task.requirement_id)
        if requirement:
            pending = session.scalar(select(func.count()).select_from(RequirementRepository).where(RequirementRepository.requirement_id == requirement.id, RequirementRepository.status != "merged")) or 0
            event = "repository_merged" if pending else "all_repositories_merged"
            transition_requirement(session, requirement, event, requirement.version, "git_worker", None, str(output.get("merged_sha", "")))
        return

    if task.task_type == "git.publish_changes":
        for item in output.get("repositories", []):
            link = session.get(RequirementRepository, item.get("requirement_repository_id"))
            if link and link.requirement_id == task.requirement_id:
                link.work_branch = item.get("work_branch")
                link.pull_request_number = item.get("pull_request_number")
                link.pull_request_url = item.get("pull_request_url")
                link.head_sha = item.get("head_sha")
                link.status = "ready"

    max_version = session.scalar(
        select(func.max(ArtifactVersion.version)).where(
            ArtifactVersion.requirement_id == task.requirement_id,
            ArtifactVersion.kind == ARTIFACT_KINDS.get(task.task_type, task.task_type),
        )
    ) or 0
    session.add(
        ArtifactVersion(
            requirement_id=task.requirement_id,
            agent_run_id=task.agent_run_id,
            kind=ARTIFACT_KINDS.get(task.task_type, task.task_type),
            version=max_version + 1,
            content_json=json.dumps(output, ensure_ascii=False),
            content_markdown=_to_markdown(output),
        )
    )
    requirement = session.get(Requirement, task.requirement_id)
    if requirement is None:
        return
    if task.task_type == "agent.review":
        event = "review_approved" if output.get("approved") else "review_rejected"
    elif task.task_type == "agent.accept":
        event = "acceptance_approved" if output.get("approved") else "acceptance_rejected"
    elif task.task_type == "agent.final_accept":
        event = "final_acceptance_passed" if output.get("approved") else "final_acceptance_failed"
    elif task.task_type == "agent.regression":
        event = "regression_passed" if output.get("approved") else "regression_failed"
    else:
        event = TASK_EVENTS.get(task.task_type)
    if event:
        transition_requirement(
            session,
            requirement,
            event,
            requirement.version,
            actor_type="agent",
            actor_id=task.agent_run_id,
            reason=str(output.get("summary", "")),
        )


def _to_markdown(value: Any, level: int = 1) -> str:
    if isinstance(value, dict):
        lines: list[str] = []
        for key, item in value.items():
            title = key.replace("_", " ").title()
            lines.append(f"{'#' * min(level, 6)} {title}")
            lines.append(_to_markdown(item, level + 1))
        return "\n\n".join(lines)
    if isinstance(value, list):
        return "\n".join(f"- {_to_markdown(item, level + 1)}" for item in value) or "- 无"
    return str(value)


def _externalize_large_evidence(
    session: Session,
    task: WorkflowTask,
    output: dict[str, Any],
) -> None:
    if task.task_type != "git.publish_changes":
        return
    combined_diff = output.get("combined_diff")
    if not isinstance(combined_diff, str):
        return
    encoded = combined_diff.encode("utf-8")
    settings = get_settings()
    if len(encoded) <= settings.artifact_inline_max_bytes:
        return
    path, digest, size = ArtifactStore(settings.artifact_root).write(
        task.requirement_id,
        "delivery-diff",
        encoded,
    )
    session.add(
        Evidence(
            requirement_id=task.requirement_id,
            agent_run_id=task.agent_run_id,
            kind="delivery_diff",
            path=path,
            sha256=digest,
            size_bytes=size,
        )
    )
    excerpt = encoded[: settings.artifact_inline_max_bytes].decode("utf-8", errors="replace")
    output["combined_diff"] = excerpt + "\n[full diff stored as immutable evidence]"
    output["combined_diff_evidence"] = {"path": path, "sha256": digest, "size_bytes": size}


def _schedule_retry(session: Session, task: WorkflowTask, run: AgentRun | None) -> WorkflowTask:
    payload = json.loads(task.payload_json)
    retry_run = None
    if run:
        retry_run = AgentRun(
            requirement_id=run.requirement_id,
            agent_key=run.agent_key,
            role=run.role,
            model=run.model,
            prompt_version=run.prompt_version,
            input_json=run.input_json,
        )
        session.add(retry_run)
        session.flush()
        payload["agent_run_id"] = retry_run.id
    delay = min(get_settings().task_retry_base_seconds * (2 ** (task.attempt_count - 1)), 60)
    retry_task = WorkflowTask(
        requirement_id=task.requirement_id,
        agent_run_id=retry_run.id if retry_run else None,
        task_type=task.task_type,
        status="queued",
        idempotency_key=f"{task.idempotency_key}:retry:{task.attempt_count}",
        payload_json=json.dumps(payload, ensure_ascii=False),
        attempt_count=task.attempt_count,
        available_at=datetime.now(UTC) + timedelta(seconds=delay),
    )
    session.add(retry_task)
    session.flush()
    session.add(
        OutboxEvent(
            topic="forgeflow.tasks",
            aggregate_id=task.requirement_id,
            payload_json=json.dumps(
                {"task_id": retry_task.id, "task_type": retry_task.task_type, "payload": payload},
                ensure_ascii=False,
            ),
        )
    )
    return retry_task
