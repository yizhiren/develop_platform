import json
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..core.config import get_settings
from ..models.entities import AgentRun, ArtifactVersion, Evidence, MergeAttempt, OutboxEvent, Requirement, RequirementRepository, WorkflowTask
from .artifacts import ArtifactStore
from .diagnostics import safe_error_message
from .workflow import transition_requirement


TASK_EVENTS = {
    "agent.clarify": "clarification_ready",
    "agent.architect": "plan_ready",
    "agent.develop": "development_ready",
    "agent.revise": "revision_ready",
    "agent.final_accept": "final_acceptance_passed",
    "git.prepare_workspaces": "workspace_ready",
    "git.restore_workspaces": "workspace_restored",
    "git.restore_validation_workspace": "validation_workspace_restored",
    "dependency.prepare": "dependencies_ready",
    "dependency.prepare_verification": "verification_dependencies_ready",
    "dependency.prepare_incremental_verification": "incremental_verification_dependencies_ready",
    "dependency.prepare_final_verification": "final_verification_dependencies_ready",
    "git.prepare_analysis": "analysis_ready",
    "git.commit_changes": "changes_committed",
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
    "git.restore_workspaces": "workspace_manifest",
    "git.restore_validation_workspace": "workspace_manifest",
    "dependency.prepare": "dependency_manifest",
    "dependency.prepare_verification": "verification_dependency_manifest",
    "dependency.prepare_incremental_verification": "incremental_verification_dependency_manifest",
    "dependency.prepare_final_verification": "final_verification_dependency_manifest",
    "git.prepare_analysis": "repository_analysis",
    "git.commit_changes": "development_commit_manifest",
    "git.publish_changes": "delivery_manifest",
    "git.create_pull_request": "pull_request_manifest",
    "git.prepare_verification": "verification_manifest",
    "git.prepare_final_verification": "final_verification_manifest",
    "git.prepare_incremental_verification": "incremental_verification_manifest",
    "agent.regression": "incremental_regression_report",
}


def process_task_result(session: Session, result: dict[str, Any]) -> None:
    task = session.get(WorkflowTask, result["task_id"])
    # Workers can finish after a requester closes the requirement. Only active
    # tasks may mutate artifacts or advance the workflow; late results are inert.
    if task is None or task.status not in {"queued", "running"}:
        return
    run = session.get(AgentRun, task.agent_run_id) if task.agent_run_id else None
    result_status = str(result.get("status", "failed"))
    if result_status == "running":
        _mark_task_running(task, run, result)
        return
    if result_status != "completed":
        task.status = "failed"
        task.lease_owner = None
        task.lease_expires_at = None
        task.attempt_count += 1
        payload = json.loads(task.payload_json)
        failure = {
            "error_code": str(result.get("error_code", "worker.failed"))[:80],
            "error_message": safe_error_message(result.get("error_message", ""), 3_800),
            "changed_paths": [
                str(path)[:1_000]
                for path in result.get("changed_paths", [])[:200]
                if isinstance(path, str)
            ]
            if isinstance(result.get("changed_paths"), list)
            else [],
        }
        diagnostics = _safe_agent_diagnostics(result.get("diagnostics"))
        if diagnostics:
            failure["diagnostics"] = diagnostics
        payload["_failure"] = failure
        task.payload_json = json.dumps(payload, ensure_ascii=False)
        if run:
            run.status = "failed"
            if result.get("model"):
                run.model = str(result["model"])[:120]
            run.error_code = result.get("error_code", "worker.failed")
            run.error_message = _failure_reason(result)
            run.diagnostics_json = json.dumps(
                _safe_agent_diagnostics(result.get("diagnostics")),
                ensure_ascii=False,
            )
            run.token_usage = int(result.get("token_usage", 0))
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
                transition_requirement(session, requirement, "merge_failed", requirement.version, "worker", None, _failure_reason(result))
        elif task.task_type.startswith("dependency.prepare"):
            requirement = session.get(Requirement, task.requirement_id)
            if requirement:
                transition_requirement(
                    session,
                    requirement,
                    "dependency_failed",
                    requirement.version,
                    "dependency_worker",
                    None,
                    _failure_reason(result),
                )
        elif task.task_type in {
            "git.prepare_workspaces",
            "git.restore_workspaces",
            "git.restore_validation_workspace",
            "git.commit_changes",
            "git.publish_changes",
        }:
            requirement = session.get(Requirement, task.requirement_id)
            if requirement:
                transition_requirement(
                    session,
                    requirement,
                    "automation_failed",
                    requirement.version,
                    "git_worker",
                    None,
                    _failure_reason(result),
                )
        elif task.task_type == "git.create_pull_request":
            # Stay at the merge gate so the owner can retry or use the
            # validated manual registration fallback.
            return
        elif task.task_type in {
            "git.prepare_analysis",
            "git.prepare_verification",
            "git.prepare_final_verification",
            "git.prepare_incremental_verification",
        }:
            requirement = session.get(Requirement, task.requirement_id)
            if requirement:
                transition_requirement(session, requirement, "technical_failure", requirement.version, "git_worker", None, _failure_reason(result))
        elif task.task_type.startswith("agent."):
            requirement = session.get(Requirement, task.requirement_id)
            if requirement:
                transition_requirement(
                    session,
                    requirement,
                    "technical_failure",
                    requirement.version,
                    "worker",
                    run.id if run else None,
                    _failure_reason(result),
                )
        return

    output = dict(result.get("output") or {})
    if task.task_type in {"agent.accept", "agent.final_accept", "agent.regression"}:
        _enforce_acceptance_evidence_gate(task, output)
    _externalize_large_evidence(session, task, output)
    task.status = "completed"
    task.lease_owner = None
    task.lease_expires_at = None
    if run:
        run.status = "completed"
        if result.get("model"):
            run.model = str(result["model"])[:120]
        run.output_json = json.dumps(output, ensure_ascii=False)
        run.diagnostics_json = json.dumps(
            _safe_agent_diagnostics(result.get("diagnostics")),
            ensure_ascii=False,
        )
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

    if task.task_type in {"git.commit_changes", "git.publish_changes"}:
        for item in output.get("repositories", []):
            link = session.get(RequirementRepository, item.get("requirement_repository_id"))
            if link and link.requirement_id == task.requirement_id:
                link.work_branch = item.get("work_branch")
                if task.task_type == "git.publish_changes":
                    link.pull_request_number = item.get("pull_request_number")
                    link.pull_request_url = item.get("pull_request_url")
                link.head_sha = item.get("head_sha")
                link.status = "ready" if task.task_type == "git.publish_changes" else "committed"
    elif task.task_type == "git.create_pull_request":
        link = session.get(RequirementRepository, output.get("requirement_repository_id"))
        if link and link.requirement_id == task.requirement_id:
            if str(output.get("head_sha", "")).lower() != (link.head_sha or "").lower():
                raise ValueError("pull request result head SHA does not match reviewed delivery")
            link.pull_request_number = int(output["pull_request_number"])
            link.pull_request_url = str(output["pull_request_url"])

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
    if task.task_type == "agent.clarify":
        event = "clarification_ready" if output.get("open_questions") else "clarification_complete"
    elif task.task_type == "agent.review":
        event = "review_approved" if output.get("approved") else "review_rejected"
    elif task.task_type == "agent.develop" and not output.get("repositories_changed"):
        prior_commit = session.scalar(
            select(ArtifactVersion.id)
            .where(
                ArtifactVersion.requirement_id == task.requirement_id,
                ArtifactVersion.kind == "development_commit_manifest",
            )
            .limit(1)
        )
        event = "development_evidence_ready" if prior_commit else TASK_EVENTS.get(task.task_type)
    elif task.task_type == "agent.accept":
        event = "acceptance_approved" if output.get("approved") else "acceptance_rejected"
    elif task.task_type == "agent.final_accept":
        event = "final_acceptance_passed" if output.get("approved") else "final_acceptance_failed"
    elif task.task_type == "agent.regression":
        event = "regression_passed" if output.get("approved") else "regression_failed"
    else:
        event = TASK_EVENTS.get(task.task_type)
    if event:
        task_context: dict[str, Any] | None = None
        if task.task_type == "dependency.prepare":
            payload = json.loads(task.payload_json)
            previous_failure = payload.get("context", {}).get("_previous_attempt_failure")
            if isinstance(previous_failure, dict):
                task_context = {"_previous_attempt_failure": previous_failure}
        transition_requirement(
            session,
            requirement,
            event,
            requirement.version,
            actor_type="agent",
            actor_id=task.agent_run_id,
            reason=str(output.get("summary", "")),
            task_context=task_context,
        )
        if task.task_type == "agent.architect":
            _auto_approve_architecture_plan(session, requirement, output)


def _auto_approve_architecture_plan(
    session: Session,
    requirement: Requirement,
    output: dict[str, Any],
) -> None:
    confidence = output.get("confidence")
    # Runtime schema validation guarantees an integer for new artifacts. Keep
    # historical/malformed artifacts on the safer manual-review path.
    if type(confidence) is not int:
        return
    threshold = get_settings().architecture_auto_approve_confidence_threshold
    if confidence <= threshold:
        return
    transition_requirement(
        session,
        requirement,
        "confirm_plan",
        requirement.version,
        actor_type="system",
        actor_id=None,
        reason=(
            f"方案置信度 {confidence}% 高于自动批准阈值 {threshold}%，"
            "平台自动批准并开始开发"
        ),
    )


def _mark_task_running(
    task: WorkflowTask,
    run: AgentRun | None,
    result: dict[str, Any],
) -> None:
    try:
        lease_seconds = int(result.get("lease_seconds", 30))
    except (TypeError, ValueError):
        lease_seconds = 30
    lease_seconds = min(max(lease_seconds, 30), 24 * 3600)
    worker_id = str(result.get("worker_id", "")).strip()[:120]
    task.status = "running"
    task.lease_owner = worker_id or None
    task.lease_expires_at = datetime.now(UTC) + timedelta(seconds=lease_seconds)
    if run and run.status in {"queued", "running"}:
        run.status = "running"


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


def _failure_reason(result: dict[str, Any]) -> str:
    code = str(result.get("error_code", "worker.failed")).strip()[:80]
    message = safe_error_message(result.get("error_message", ""), 3_800)
    return f"{code}: {message}" if message and message != code else code


def _safe_agent_diagnostics(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, Any] = {}
    for key in ("turns", "max_turns", "tool_calls", "tool_errors"):
        try:
            result[key] = min(max(int(value.get(key) or 0), 0), 2**31 - 1)
        except (TypeError, ValueError):
            result[key] = 0
    result["last_stop_reason"] = str(value.get("last_stop_reason") or "")[:80]
    result["terminal_tool_called"] = bool(value.get("terminal_tool_called"))
    counts = value.get("tool_call_counts")
    if isinstance(counts, dict):
        safe_counts: dict[str, int] = {}
        for name, count in list(counts.items())[:100]:
            safe_name = str(name).strip()[:64]
            if not safe_name:
                continue
            try:
                safe_counts[safe_name] = min(max(int(count or 0), 0), 2**31 - 1)
            except (TypeError, ValueError):
                safe_counts[safe_name] = 0
        result["tool_call_counts"] = safe_counts
    return result


def _enforce_acceptance_evidence_gate(task: WorkflowTask, output: dict[str, Any]) -> None:
    """Reject unsupported Agent4 approvals before they can advance the workflow."""
    payload = json.loads(task.payload_json)
    context = payload.get("context", {})
    artifacts = context.get("artifacts", {})
    manifest_key = {
        "agent.accept": "verification_manifest",
        "agent.final_accept": "final_verification_manifest",
        "agent.regression": "incremental_verification_manifest",
    }[task.task_type]
    if not artifacts.get(manifest_key):
        # Legacy/Fake flows without repository automation retain the protocol-only behavior.
        return

    clarification = artifacts.get("clarification_spec", {})
    expected = clarification.get("acceptance_criteria", []) if isinstance(clarification, dict) else []
    expected = [item for item in expected if isinstance(item, dict)]
    expected_ids = [str(item.get("id", "")).strip() for item in expected]
    priorities = {
        str(item.get("id", "")).strip(): str(item.get("priority", "must"))
        for item in expected
    }
    results = output.get("criteria", [])
    results = [item for item in results if isinstance(item, dict)] if isinstance(results, list) else []
    result_ids = [str(item.get("criterion_id", "")).strip() for item in results]
    raw_evidence = output.get("regression_results", [])
    evidence = [item for item in raw_evidence if isinstance(item, dict)] if isinstance(raw_evidence, list) else []
    evidence_by_id = {
        str(item.get("evidence_id")): item
        for item in evidence
        if item.get("evidence_id")
    }

    issues: list[str] = []
    if not expected_ids or any(not item for item in expected_ids) or len(set(expected_ids)) != len(expected_ids):
        issues.append("confirmed acceptance criteria are missing or have invalid ids")
    if len(result_ids) != len(set(result_ids)) or set(result_ids) != set(expected_ids):
        issues.append(
            f"acceptance report must exactly cover confirmed criteria; expected={expected_ids}, actual={result_ids}"
        )
    for item in results:
        criterion_id = str(item.get("criterion_id", "")).strip()
        if item.get("status") != "passed" or criterion_id not in priorities:
            continue
        evidence_paths = [str(value) for value in item.get("evidence_paths", [])]
        unknown_evidence = [value for value in evidence_paths if value not in evidence_by_id]
        if unknown_evidence:
            issues.append(f"{criterion_id} references unknown evidence ids: {unknown_evidence}")
        linked = [evidence_by_id.get(value) for value in evidence_paths]
        direct = [
            value
            for value in linked
            if value
            and criterion_id in value.get("criterion_ids", [])
            and value.get("status") == "passed"
        ]
        if not direct:
            issues.append(f"{criterion_id} is passed without directly linked passing evidence")

    if output.get("approved"):
        independent_commands = [
            item
            for item in evidence
            if item.get("source") == "agent4_independent"
            and item.get("type") == "command"
            and item.get("status") == "passed"
        ]
        if not independent_commands:
            issues.append("approval requires at least one successful independent Agent4 command")
        failed_commands = [
            item
            for item in evidence
            if item.get("type") == "command" and item.get("status") != "passed"
        ]
        if failed_commands:
            issues.append("approval contains failed replay or independent commands")
        if any(item.get("workspace_integrity_violations") for item in evidence):
            issues.append("approval contains workspace integrity violations")
        failed_must = [
            item.get("criterion_id")
            for item in results
            if priorities.get(str(item.get("criterion_id", ""))) == "must"
            and item.get("status") != "passed"
        ]
        if failed_must:
            issues.append(f"must criteria are not passed: {failed_must}")

    evidence.append(
        {
            "evidence_id": "platform-acceptance-gate",
            "source": "platform_gate",
            "type": "acceptance_coverage",
            "status": "failed" if issues else "passed",
            "issues": issues,
        }
    )
    output["regression_results"] = evidence
    if issues:
        output["approved"] = False
        detail = "; ".join(issues)
        original = str(output.get("summary", "")).strip()
        output["summary"] = f"平台验收证据门禁未通过：{detail}。{original}".strip()


def _externalize_large_evidence(
    session: Session,
    task: WorkflowTask,
    output: dict[str, Any],
) -> None:
    if task.task_type not in {"git.commit_changes", "git.publish_changes"}:
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
    if isinstance(payload.get("context"), dict) and isinstance(payload.get("_failure"), dict):
        payload["context"] = {
            **payload["context"],
            "_previous_attempt_failure": payload["_failure"],
        }
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
