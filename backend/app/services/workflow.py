import json
from dataclasses import dataclass
from typing import Final

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models.entities import (
    AgentRun,
    ArtifactVersion,
    OutboxEvent,
    RepositoryConnection,
    Requirement,
    RequirementRepository,
    RequirementStatus,
    WorkflowTask,
    WorkflowTransition,
)
from ..core.config import get_settings


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
        "workspace_ready": TransitionRule(RequirementStatus.DEVELOPING, "agent.develop"),
        "development_ready": TransitionRule(RequirementStatus.REVIEWING, "agent.review"),
        "changes_published": TransitionRule(RequirementStatus.REVIEWING, "agent.review"),
        "automation_failed": TransitionRule(RequirementStatus.BLOCKED),
        "cancel": TransitionRule(RequirementStatus.CANCELLED),
    },
    RequirementStatus.REVIEWING: {
        "review_approved": TransitionRule(RequirementStatus.ACCEPTING, "agent.accept"),
        "review_rejected": TransitionRule(RequirementStatus.DEVELOPING, "agent.develop"),
        "cancel": TransitionRule(RequirementStatus.CANCELLED),
    },
    RequirementStatus.ACCEPTING: {
        "verification_ready": TransitionRule(RequirementStatus.ACCEPTING, "agent.accept"),
        "acceptance_approved": TransitionRule(RequirementStatus.AWAITING_MERGE),
        "acceptance_rejected": TransitionRule(RequirementStatus.REPLANNING, "agent.revise"),
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
        "incremental_verification_ready": TransitionRule(RequirementStatus.REGRESSION, "agent.regression"),
        "regression_passed": TransitionRule(RequirementStatus.AWAITING_MERGE),
        "regression_failed": TransitionRule(RequirementStatus.BLOCKED),
        "cancel": TransitionRule(RequirementStatus.CANCELLED),
    },
    RequirementStatus.FINAL_ACCEPTANCE: {
        "final_verification_ready": TransitionRule(RequirementStatus.FINAL_ACCEPTANCE, "agent.final_accept"),
        "final_acceptance_passed": TransitionRule(RequirementStatus.COMPLETED),
        "final_acceptance_failed": TransitionRule(RequirementStatus.BLOCKED),
    },
    RequirementStatus.BLOCKED: {
        "retry_development": TransitionRule(RequirementStatus.DEVELOPING, "agent.develop"),
        "retry_planning": TransitionRule(RequirementStatus.REPLANNING, "agent.revise"),
        "retry_merge": TransitionRule(RequirementStatus.AWAITING_MERGE),
        "retry_regression": TransitionRule(RequirementStatus.REGRESSION, "git.prepare_incremental_verification"),
        "cancel": TransitionRule(RequirementStatus.CANCELLED),
    },
}

PAUSABLE = set(RULES) - {RequirementStatus.BLOCKED}
AGENT_IDENTITIES = {
    "clarify": "agent1",
    "architect": "agent2",
    "review": "agent2",
    "revise": "agent2",
    "develop": "agent3",
    "accept": "agent4",
    "final_accept": "agent4",
    "regression": "agent4",
}


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
    if event == "pause" and current in PAUSABLE:
        requirement.paused_from = current.value
        rule = TransitionRule(RequirementStatus.PAUSED)
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
        if current == RequirementStatus.AWAITING_CLARIFICATION and event == "confirm_clarification":
            rule = TransitionRule(RequirementStatus.PLANNING, "git.prepare_analysis")
        elif current == RequirementStatus.AWAITING_PLAN and event == "confirm_plan":
            rule = TransitionRule(RequirementStatus.DEVELOPING, "git.prepare_workspaces")
        elif current == RequirementStatus.DEVELOPING and event == "development_ready":
            rule = TransitionRule(RequirementStatus.DEVELOPING, "git.publish_changes")
        elif current == RequirementStatus.REVIEWING and event == "review_approved":
            rule = TransitionRule(RequirementStatus.ACCEPTING, "git.prepare_verification")
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

    previous = requirement.status
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

    task = None
    if rule.task_type:
        role = rule.task_type.split(".", 1)[1] if rule.task_type.startswith("agent.") else None
        context = _build_task_context(session, requirement)
        if task_context:
            context.update(task_context)
        agent_run = None
        if role:
            agent_run = AgentRun(
                requirement_id=requirement.id,
                agent_key=AGENT_IDENTITIES[role],
                role=role,
                model=get_settings().llm_model if get_settings().llm_provider != "fake" else "fake",
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


def _build_task_context(session: Session, requirement: Requirement) -> dict:
    context: dict = {
        "requirement_id": requirement.id,
        "title": requirement.title,
        "description": requirement.description,
        "repositories": [],
        "artifacts": {},
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
    artifacts = session.scalars(
        select(ArtifactVersion)
        .where(ArtifactVersion.requirement_id == requirement.id)
        .order_by(ArtifactVersion.created_at.desc())
    ).all()
    for artifact in artifacts:
        if artifact.kind not in context["artifacts"]:
            context["artifacts"][artifact.kind] = json.loads(artifact.content_json)
    return context
