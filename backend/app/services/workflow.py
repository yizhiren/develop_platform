import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models.entities import (
    AgentRun,
    ArtifactVersion,
    ConversationMessage,
    OutboxEvent,
    RepositoryConnection,
    Requirement,
    RequirementAttachment,
    RequirementRepository,
    RequirementStatus,
    WorkflowTask,
    WorkflowTransition,
)
from ..core.config import get_settings
from ..agents.roles import ROLE_TO_AGENT_KEY


class WorkflowError(ValueError):
    pass


class VersionConflict(WorkflowError):
    pass


@dataclass(frozen=True)
class TransitionRule:
    target: RequirementStatus
    task_type: str | None = None


RULES: Final[dict[RequirementStatus, dict[str, TransitionRule]]] = {
    RequirementStatus.DRAFT: {
        "publish": TransitionRule(RequirementStatus.CLARIFYING, "agent.clarify"),
        "cancel": TransitionRule(RequirementStatus.CANCELLED),
    },
    RequirementStatus.CLARIFYING: {
        "clarification_ready": TransitionRule(RequirementStatus.AWAITING_CLARIFICATION),
        "clarification_complete": TransitionRule(RequirementStatus.PLANNING, "agent.architect"),
        "cancel": TransitionRule(RequirementStatus.CANCELLED),
    },
    RequirementStatus.AWAITING_CLARIFICATION: {
        "confirm_clarification": TransitionRule(RequirementStatus.PLANNING, "agent.architect"),
        "request_more_clarification": TransitionRule(RequirementStatus.CLARIFYING, "agent.clarify"),
        "cancel": TransitionRule(RequirementStatus.CANCELLED),
    },
    RequirementStatus.PLANNING: {
        "analysis_ready": TransitionRule(RequirementStatus.PLANNING, "agent.architect"),
        "plan_ready": TransitionRule(RequirementStatus.AWAITING_PLAN),
        "cancel": TransitionRule(RequirementStatus.CANCELLED),
    },
    RequirementStatus.AWAITING_PLAN: {
        "confirm_plan": TransitionRule(RequirementStatus.DEVELOPING, "agent.develop"),
        "request_plan_change": TransitionRule(RequirementStatus.PLANNING, "agent.architect"),
        "cancel": TransitionRule(RequirementStatus.CANCELLED),
    },
    RequirementStatus.DEVELOPING: {
        "workspace_ready": TransitionRule(RequirementStatus.DEVELOPING, "dependency.prepare"),
        "workspace_restored": TransitionRule(RequirementStatus.DEVELOPING, "dependency.prepare"),
        "dependencies_ready": TransitionRule(RequirementStatus.DEVELOPING, "agent.develop"),
        "development_ready": TransitionRule(RequirementStatus.REVIEWING, "agent.review"),
        "development_evidence_ready": TransitionRule(
            RequirementStatus.DEVELOPING,
            "git.restore_validation_workspace",
        ),
        "validation_workspace_restored": TransitionRule(RequirementStatus.REVIEWING, "agent.review"),
        "changes_committed": TransitionRule(RequirementStatus.REVIEWING, "agent.review"),
        "changes_published": TransitionRule(RequirementStatus.REVIEWING, "agent.review"),
        "dependency_failed": TransitionRule(RequirementStatus.BLOCKED),
        "automation_failed": TransitionRule(RequirementStatus.BLOCKED),
        "cancel": TransitionRule(RequirementStatus.CANCELLED),
    },
    RequirementStatus.REVIEWING: {
        "review_approved": TransitionRule(RequirementStatus.ACCEPTING, "agent.accept"),
        "review_rejected": TransitionRule(RequirementStatus.DEVELOPING, "agent.develop"),
        "changes_published": TransitionRule(RequirementStatus.ACCEPTING, "git.prepare_verification"),
        "automation_failed": TransitionRule(RequirementStatus.BLOCKED),
        "cancel": TransitionRule(RequirementStatus.CANCELLED),
    },
    RequirementStatus.ACCEPTING: {
        "verification_ready": TransitionRule(
            RequirementStatus.ACCEPTING,
            "dependency.prepare_verification",
        ),
        "verification_dependencies_ready": TransitionRule(
            RequirementStatus.ACCEPTING,
            "agent.accept",
        ),
        "acceptance_approved": TransitionRule(RequirementStatus.AWAITING_MERGE),
        "acceptance_rejected": TransitionRule(RequirementStatus.REPLANNING, "agent.revise"),
        "dependency_failed": TransitionRule(RequirementStatus.BLOCKED),
        "cancel": TransitionRule(RequirementStatus.CANCELLED),
    },
    RequirementStatus.REPLANNING: {
        "revision_ready": TransitionRule(RequirementStatus.DEVELOPING, "agent.develop"),
        "cancel": TransitionRule(RequirementStatus.CANCELLED),
    },
    RequirementStatus.AWAITING_MERGE: {
        "begin_merge": TransitionRule(RequirementStatus.MERGING, "git.merge_next"),
        "cancel": TransitionRule(RequirementStatus.CANCELLED),
    },
    RequirementStatus.MERGING: {
        "repository_merged": TransitionRule(RequirementStatus.AWAITING_MERGE),
        "all_repositories_merged": TransitionRule(RequirementStatus.FINAL_ACCEPTANCE, "agent.final_accept"),
        "merge_failed": TransitionRule(RequirementStatus.BLOCKED),
    },
    RequirementStatus.REGRESSION: {
        "incremental_verification_ready": TransitionRule(
            RequirementStatus.REGRESSION,
            "dependency.prepare_incremental_verification",
        ),
        "incremental_verification_dependencies_ready": TransitionRule(
            RequirementStatus.REGRESSION,
            "agent.regression",
        ),
        "regression_passed": TransitionRule(RequirementStatus.AWAITING_MERGE),
        "regression_failed": TransitionRule(RequirementStatus.BLOCKED),
        "dependency_failed": TransitionRule(RequirementStatus.BLOCKED),
        "cancel": TransitionRule(RequirementStatus.CANCELLED),
    },
    RequirementStatus.FINAL_ACCEPTANCE: {
        "final_verification_ready": TransitionRule(
            RequirementStatus.FINAL_ACCEPTANCE,
            "dependency.prepare_final_verification",
        ),
        "final_verification_dependencies_ready": TransitionRule(
            RequirementStatus.FINAL_ACCEPTANCE,
            "agent.final_accept",
        ),
        "final_acceptance_passed": TransitionRule(RequirementStatus.COMPLETED),
        "final_acceptance_failed": TransitionRule(RequirementStatus.BLOCKED),
        "dependency_failed": TransitionRule(RequirementStatus.BLOCKED),
        "cancel": TransitionRule(RequirementStatus.CANCELLED),
    },
    RequirementStatus.BLOCKED: {
        "retry_development": TransitionRule(RequirementStatus.DEVELOPING, "agent.develop"),
        "retry_planning": TransitionRule(RequirementStatus.REPLANNING, "agent.revise"),
        "retry_acceptance": TransitionRule(RequirementStatus.ACCEPTING, "git.prepare_verification"),
        "retry_merge": TransitionRule(RequirementStatus.AWAITING_MERGE),
        "retry_regression": TransitionRule(RequirementStatus.REGRESSION, "git.prepare_incremental_verification"),
        "cancel": TransitionRule(RequirementStatus.CANCELLED),
    },
}

PAUSABLE = set(RULES) - {RequirementStatus.BLOCKED, RequirementStatus.MERGING}


def transition_requirement(
    session: Session,
    requirement: Requirement,
    event: str,
    expected_version: int,
    actor_type: str,
    actor_id: str | None,
    reason: str = "",
    task_context: dict | None = None,
) -> WorkflowTask | None:
    if requirement.version != expected_version:
        raise VersionConflict(
            f"expected requirement version {expected_version}, current is {requirement.version}"
        )

    current = RequirementStatus(requirement.status)
    previous_development_failure = (
        _latest_development_failure(session, requirement.id)
        if current == RequirementStatus.BLOCKED and event == "retry_development"
        else {}
    )
    if current == RequirementStatus.AWAITING_CLARIFICATION and event == "request_more_clarification" and not reason.strip():
        raise WorkflowError("clarification feedback is required")
    if current == RequirementStatus.AWAITING_PLAN and event == "request_plan_change" and not reason.strip():
        raise WorkflowError("architecture plan feedback is required")
    if event == "cancel" and not reason.strip():
        raise WorkflowError("closing reason is required")
    if current == RequirementStatus.AWAITING_CLARIFICATION and event == "confirm_clarification":
        clarification = session.scalar(
            select(ArtifactVersion)
            .where(
                ArtifactVersion.requirement_id == requirement.id,
                ArtifactVersion.kind == "clarification_spec",
            )
            .order_by(ArtifactVersion.version.desc())
        )
        if clarification and json.loads(clarification.content_json).get("open_questions"):
            raise WorkflowError("clarification still has open questions")
    if event == "pause" and current in PAUSABLE:
        requirement.paused_from = current.value
        rule = TransitionRule(RequirementStatus.PAUSED)
    elif current == RequirementStatus.PAUSED and event == "cancel":
        rule = TransitionRule(RequirementStatus.CANCELLED)
        requirement.paused_from = None
    elif current == RequirementStatus.PAUSED and event == "resume" and requirement.paused_from:
        rule = TransitionRule(RequirementStatus(requirement.paused_from))
        requirement.paused_from = None
    elif event == "technical_failure" and current not in {
        RequirementStatus.COMPLETED,
        RequirementStatus.CANCELLED,
        RequirementStatus.BLOCKED,
    }:
        rule = TransitionRule(RequirementStatus.BLOCKED)
    else:
        rule = RULES.get(current, {}).get(event)
        if rule is None:
            raise WorkflowError(f"event {event!r} is not valid from {current.value!r}")

    settings = get_settings()
    if settings.repository_automation_enabled:
        if current == RequirementStatus.CLARIFYING and event == "clarification_complete":
            rule = TransitionRule(RequirementStatus.PLANNING, "git.prepare_analysis")
        elif current == RequirementStatus.AWAITING_CLARIFICATION and event == "confirm_clarification":
            rule = TransitionRule(RequirementStatus.PLANNING, "git.prepare_analysis")
        elif current == RequirementStatus.AWAITING_PLAN and event == "confirm_plan":
            rule = TransitionRule(RequirementStatus.DEVELOPING, "git.prepare_workspaces")
        elif current == RequirementStatus.REPLANNING and event == "revision_ready":
            workspace_task = (
                "git.restore_workspaces"
                if _has_artifact(session, requirement.id, "workspace_manifest")
                else "git.prepare_workspaces"
            )
            rule = TransitionRule(RequirementStatus.DEVELOPING, workspace_task)
        elif (
            current == RequirementStatus.BLOCKED
            and event == "retry_development"
            and not _has_artifact(session, requirement.id, "workspace_manifest")
        ):
            rule = TransitionRule(RequirementStatus.DEVELOPING, "git.prepare_workspaces")
        elif current == RequirementStatus.BLOCKED and event == "retry_development":
            rule = TransitionRule(
                RequirementStatus.DEVELOPING,
                "dependency.prepare"
                if previous_development_failure.get("changed_paths")
                else "git.restore_workspaces",
            )
        elif current == RequirementStatus.REVIEWING and event == "review_rejected":
            workspace_task = (
                "git.restore_workspaces"
                if _has_artifact(session, requirement.id, "workspace_manifest")
                else "git.prepare_workspaces"
            )
            rule = TransitionRule(RequirementStatus.DEVELOPING, workspace_task)
        elif current == RequirementStatus.DEVELOPING and event == "development_ready":
            rule = TransitionRule(RequirementStatus.DEVELOPING, "git.commit_changes")
        elif current == RequirementStatus.DEVELOPING and event == "development_evidence_ready":
            rule = TransitionRule(
                RequirementStatus.DEVELOPING,
                "git.restore_validation_workspace",
            )
        elif current == RequirementStatus.REVIEWING and event == "review_approved":
            rule = TransitionRule(RequirementStatus.REVIEWING, "git.publish_changes")
        elif current == RequirementStatus.MERGING and event == "all_repositories_merged":
            rule = TransitionRule(RequirementStatus.FINAL_ACCEPTANCE, "git.prepare_final_verification")
        elif current == RequirementStatus.MERGING and event == "repository_merged":
            rule = TransitionRule(RequirementStatus.REGRESSION, "git.prepare_incremental_verification")

    if current == RequirementStatus.REVIEWING and event == "review_rejected":
        requirement.review_failures += 1
        if requirement.review_failures >= 3:
            rule = TransitionRule(RequirementStatus.BLOCKED)
            reason = reason or "code review failed three times"
    if current == RequirementStatus.ACCEPTING and event == "acceptance_rejected":
        requirement.acceptance_failures += 1
        if requirement.acceptance_failures >= 3:
            rule = TransitionRule(RequirementStatus.BLOCKED)
            reason = reason or "acceptance failed three times"
    if current == RequirementStatus.BLOCKED and event == "retry_development":
        requirement.review_failures = 0
    elif current == RequirementStatus.BLOCKED and event == "retry_planning":
        requirement.review_failures = 0
        requirement.acceptance_failures = 0
    elif current == RequirementStatus.BLOCKED and event == "retry_acceptance":
        requirement.acceptance_failures = 0

    previous = requirement.status
    if (
        (
            current == RequirementStatus.AWAITING_CLARIFICATION
            and event == "request_more_clarification"
        )
        or (
            current == RequirementStatus.AWAITING_PLAN
            and event == "request_plan_change"
        )
    ) and (
        actor_type == "user"
        and reason.strip()
    ):
        session.add(
            ConversationMessage(
                requirement_id=requirement.id,
                author_type="user",
                author_id=actor_id,
                stage=current.value,
                body=reason.strip(),
            )
        )
        session.flush()
    requirement.status = rule.target.value
    requirement.version += 1
    transition = WorkflowTransition(
        requirement_id=requirement.id,
        from_status=previous,
        to_status=requirement.status,
        event=event,
        actor_type=actor_type,
        actor_id=actor_id,
        reason=reason,
    )
    session.add(transition)

    if event == "cancel":
        requirement.paused_from = None
        _cancel_pending_work(session, requirement.id, reason.strip())

    task = None
    if rule.task_type:
        role = rule.task_type.split(".", 1)[1] if rule.task_type.startswith("agent.") else None
        context = _build_task_context(session, requirement)
        if previous_development_failure:
            context["_previous_attempt_failure"] = previous_development_failure
        if role == "review":
            # A previous rejection is feedback for the developer, not evidence for
            # the next independent review. Keeping it here anchors the reviewer on
            # stale findings even when the latest commit manifest proves otherwise.
            context["artifacts"].pop("code_review_report", None)
            context["review_stage_policy"] = {
                "stage": "pre_publish",
                "publication_occurs_after_approval": True,
                "missing_push_is_not_a_review_finding": True,
                "remote_verification_owner": "trusted_git_worker_and_acceptance",
                "format_consistency_means": "preserve Markdown structure and style while allowing required content changes",
            }
        if task_context:
            context.update(task_context)
        agent_run = None
        if role:
            agent_key = ROLE_TO_AGENT_KEY[role]
            model_profile = settings.agent_model_config(agent_key)
            agent_run = AgentRun(
                requirement_id=requirement.id,
                agent_key=agent_key,
                role=role,
                model=model_profile.model if model_profile.provider != "fake" else "fake",
                input_json=json.dumps(context, ensure_ascii=False),
            )
            session.add(agent_run)
            session.flush()
        payload = {
            "requirement_id": requirement.id,
            "requirement_version": requirement.version,
            "agent_run_id": agent_run.id if agent_run else None,
            "context": context,
        }
        task = WorkflowTask(
            requirement_id=requirement.id,
            agent_run_id=agent_run.id if agent_run else None,
            task_type=rule.task_type,
            idempotency_key=f"{requirement.id}:{requirement.version}:{rule.task_type}",
            payload_json=json.dumps(payload),
        )
        session.add(task)
        session.flush()
        session.add(
            OutboxEvent(
                topic="forgeflow.tasks",
                aggregate_id=requirement.id,
                payload_json=json.dumps(
                    {"task_id": task.id, "task_type": task.task_type, "payload": payload}
                ),
            )
        )
    session.flush()
    return task


def _cancel_pending_work(
    session: Session,
    requirement_id: str,
    reason: str,
) -> None:
    """Make queued/in-flight work inert while retaining its audit history."""
    now = datetime.now(UTC)
    tasks = session.scalars(
        select(WorkflowTask).where(
            WorkflowTask.requirement_id == requirement_id,
            WorkflowTask.status.in_({"queued", "running"}),
        )
    ).all()
    task_ids = {task.id for task in tasks}
    for task in tasks:
        task.status = "cancelled"
        task.lease_owner = None
        task.lease_expires_at = None
        if task.agent_run_id:
            run = session.get(AgentRun, task.agent_run_id)
            if run and run.status in {"queued", "running"}:
                run.status = "cancelled"
                run.error_code = "requirement.cancelled"
                run.error_message = reason
                run.completed_at = now

    if not task_ids:
        return
    events = session.scalars(
        select(OutboxEvent).where(
            OutboxEvent.aggregate_id == requirement_id,
            OutboxEvent.published.is_(False),
        )
    ).all()
    for event in events:
        try:
            event_task_id = json.loads(event.payload_json).get("task_id")
        except (TypeError, json.JSONDecodeError):
            continue
        if event_task_id in task_ids:
            event.published = True
            event.published_at = now


def _has_artifact(session: Session, requirement_id: str, kind: str) -> bool:
    return session.scalar(
        select(ArtifactVersion.id)
        .where(
            ArtifactVersion.requirement_id == requirement_id,
            ArtifactVersion.kind == kind,
        )
        .limit(1)
    ) is not None


def _latest_development_failure(session: Session, requirement_id: str) -> dict:
    tasks = session.scalars(
        select(WorkflowTask)
        .where(
            WorkflowTask.requirement_id == requirement_id,
            WorkflowTask.task_type == "agent.develop",
            WorkflowTask.status == "failed",
        )
        .order_by(WorkflowTask.created_at.desc(), WorkflowTask.id.desc())
    ).all()
    for task in tasks:
        try:
            failure = json.loads(task.payload_json).get("_failure")
        except (TypeError, json.JSONDecodeError):
            continue
        if isinstance(failure, dict):
            return {
                "error_code": str(failure.get("error_code") or "")[:80],
                "error_message": str(failure.get("error_message") or "")[:3_800],
                "changed_paths": [
                    str(path)[:1_000]
                    for path in failure.get("changed_paths", [])[:200]
                    if isinstance(path, str)
                ]
                if isinstance(failure.get("changed_paths"), list)
                else [],
            }
    return {}


def _build_task_context(session: Session, requirement: Requirement) -> dict:
    context: dict = {
        "requirement_id": requirement.id,
        "title": requirement.title,
        "description": requirement.description,
        "repositories": [],
        "attachments": [],
        "artifacts": {},
        "conversation": [],
    }
    repository_rows = session.execute(
        select(RequirementRepository, RepositoryConnection)
        .join(RepositoryConnection, RepositoryConnection.id == RequirementRepository.repository_id)
        .where(RequirementRepository.requirement_id == requirement.id)
        .order_by(RequirementRepository.merge_order)
    ).all()
    context["repositories"] = [
        {
            "requirement_repository_id": link.id,
            "repository_id": repository.id,
            "provider": repository.provider,
            "full_name": repository.full_name,
            "clone_url": repository.clone_url,
            "target_branch": link.target_branch,
            "work_branch": link.work_branch,
            "pull_request_number": link.pull_request_number,
            "pull_request_url": link.pull_request_url,
            "head_sha": link.head_sha,
            "merge_order": link.merge_order,
            "status": link.status,
        }
        for link, repository in repository_rows
    ]
    attachments = session.scalars(
        select(RequirementAttachment)
        .where(RequirementAttachment.requirement_id == requirement.id)
        .order_by(RequirementAttachment.created_at, RequirementAttachment.id)
    ).all()
    context["attachments"] = [
        {
            "id": attachment.id,
            "filename": attachment.filename,
            "media_type": attachment.media_type,
            "path": attachment.path,
            "sha256": attachment.sha256,
            "size_bytes": attachment.size_bytes,
        }
        for attachment in attachments
    ]
    artifacts = session.scalars(
        select(ArtifactVersion)
        .where(ArtifactVersion.requirement_id == requirement.id)
        .order_by(ArtifactVersion.created_at.desc())
    ).all()
    for artifact in artifacts:
        if artifact.kind not in context["artifacts"]:
            context["artifacts"][artifact.kind] = json.loads(artifact.content_json)
    messages = session.scalars(
        select(ConversationMessage)
        .where(ConversationMessage.requirement_id == requirement.id)
        .order_by(ConversationMessage.created_at)
    ).all()
    context["conversation"] = [
        {
            "author_type": message.author_type,
            "stage": message.stage,
            "body": message.body,
            "created_at": message.created_at.isoformat(),
        }
        for message in messages
    ]
    return context
