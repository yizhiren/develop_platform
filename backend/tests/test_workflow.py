from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.database import Base
import json

from app.core.config import Settings
from app.models.entities import AgentRun, ArtifactVersion, ConversationMessage, MergeAttempt, OutboxEvent, Project, RepositoryConnection, Requirement, RequirementRepository, RequirementStatus, User, WorkflowTask, WorkflowTransition
from app.services.task_results import process_task_result
from app.services.workflow import VersionConflict, WorkflowError, transition_requirement


def session_with_requirement() -> tuple[Session, Requirement]:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = Session(engine)
    user = User(email="owner@example.com", display_name="Owner", password_hash="x")
    session.add(user)
    session.flush()
    project = Project(key="TEST", name="Test", owner_id=user.id)
    session.add(project)
    session.flush()
    requirement = Requirement(project_id=project.id, number=1, title="Feature", description="Detailed feature", owner_id=user.id)
    session.add(requirement)
    session.commit()
    return session, requirement


def complete_dependency_preparation(session: Session, requirement: Requirement) -> WorkflowTask:
    task = session.scalars(
        select(WorkflowTask).where(
            WorkflowTask.requirement_id == requirement.id,
            WorkflowTask.task_type == "dependency.prepare",
        )
    ).one()
    process_task_result(
        session,
        {
            "task_id": task.id,
            "status": "completed",
            "output": {
                "schema_version": "1.0",
                "scope": "development",
                "network_execution": True,
                "summary": "依赖准备完成",
                "repositories": [],
            },
        },
    )
    return task


def complete_scoped_dependency_preparation(
    session: Session,
    requirement: Requirement,
    task_type: str,
    scope: str,
) -> WorkflowTask:
    task = session.scalars(
        select(WorkflowTask).where(
            WorkflowTask.requirement_id == requirement.id,
            WorkflowTask.task_type == task_type,
        )
    ).one()
    process_task_result(
        session,
        {
            "task_id": task.id,
            "status": "completed",
            "output": {
                "schema_version": "1.0",
                "scope": scope,
                "network_execution": True,
                "summary": "依赖准备完成",
                "repositories": [],
            },
        },
    )
    return task


def test_happy_path_schedules_clarifier() -> None:
    session, requirement = session_with_requirement()
    task = transition_requirement(session, requirement, "publish", 1, "user", requirement.owner_id)
    assert requirement.status == RequirementStatus.CLARIFYING
    assert requirement.version == 2
    assert task is not None and task.task_type == "agent.clarify"
    assert session.get(AgentRun, task.agent_run_id).agent_key == "agent1"


def test_clarifier_without_open_questions_automatically_starts_architecture(monkeypatch) -> None:
    session, requirement = session_with_requirement()
    settings = Settings(_env_file=None, repository_automation_enabled=False, llm_provider="fake")
    monkeypatch.setattr("app.services.workflow.get_settings", lambda: settings)
    clarifier = transition_requirement(session, requirement, "publish", 1, "user", requirement.owner_id)

    process_task_result(
        session,
        {
            "task_id": clarifier.id,
            "status": "completed",
            "output": {"summary": "需求信息已经完整", "open_questions": []},
        },
    )

    architect = session.scalars(
        select(WorkflowTask).where(
            WorkflowTask.requirement_id == requirement.id,
            WorkflowTask.task_type == "agent.architect",
        )
    ).one()
    assert requirement.status == RequirementStatus.PLANNING
    assert architect is not None


def test_clarifier_with_open_questions_waits_for_requester(monkeypatch) -> None:
    session, requirement = session_with_requirement()
    settings = Settings(_env_file=None, repository_automation_enabled=False, llm_provider="fake")
    monkeypatch.setattr("app.services.workflow.get_settings", lambda: settings)
    clarifier = transition_requirement(session, requirement, "publish", 1, "user", requirement.owner_id)

    process_task_result(
        session,
        {
            "task_id": clarifier.id,
            "status": "completed",
            "output": {"summary": "仍需确认范围", "open_questions": ["需要处理哪些目录？"]},
        },
    )

    assert requirement.status == RequirementStatus.AWAITING_CLARIFICATION
    assert not session.scalars(
        select(WorkflowTask).where(
            WorkflowTask.requirement_id == requirement.id,
            WorkflowTask.task_type == "agent.architect",
        )
    ).all()


def test_agent_run_records_the_model_configured_for_its_stable_identity(monkeypatch) -> None:
    session, requirement = session_with_requirement()
    settings = Settings(
        _env_file=None,
        llm_provider="deepseek",
        llm_model="shared-model",
        agent1_llm_model="clarifier-model",
    )
    monkeypatch.setattr("app.services.workflow.get_settings", lambda: settings)

    task = transition_requirement(session, requirement, "publish", 1, "user", requirement.owner_id)
    run = session.get(AgentRun, task.agent_run_id)
    assert run.agent_key == "agent1"
    assert run.model == "clarifier-model"

    process_task_result(
        session,
        {
            "task_id": task.id,
            "status": "completed",
            "model": "provider-returned-model",
            "output": {"summary": "需求信息已经完整", "open_questions": []},
        },
    )
    assert run.model == "provider-returned-model"


def test_worker_start_updates_task_and_agent_run_before_terminal_result(monkeypatch) -> None:
    session, requirement = session_with_requirement()
    settings = Settings(
        _env_file=None,
        repository_automation_enabled=False,
        llm_provider="fake",
    )
    monkeypatch.setattr("app.services.workflow.get_settings", lambda: settings)
    task = transition_requirement(
        session,
        requirement,
        "publish",
        requirement.version,
        "user",
        requirement.owner_id,
    )
    run = session.get(AgentRun, task.agent_run_id)
    requirement_version = requirement.version

    process_task_result(
        session,
        {
            "task_id": task.id,
            "status": "running",
            "worker_id": "agent-worker:7",
            "lease_seconds": 300,
        },
    )

    assert task.status == "running"
    assert task.lease_owner == "agent-worker:7"
    assert task.lease_expires_at is not None
    assert run.status == "running"
    assert requirement.version == requirement_version

    process_task_result(
        session,
        {
            "task_id": task.id,
            "status": "completed",
            "output": {"summary": "还需要确认", "open_questions": ["范围是什么？"]},
        },
    )

    assert task.status == "completed"
    assert task.lease_owner is None
    assert task.lease_expires_at is None
    assert run.status == "completed"


def test_version_conflict_is_rejected() -> None:
    session, requirement = session_with_requirement()
    try:
        transition_requirement(session, requirement, "publish", 99, "user", requirement.owner_id)
    except VersionConflict:
        pass
    else:
        raise AssertionError("expected VersionConflict")


def test_review_blocks_after_three_rejections() -> None:
    session, requirement = session_with_requirement()
    requirement.status = RequirementStatus.REVIEWING
    requirement.review_failures = 2
    transition_requirement(session, requirement, "review_rejected", 1, "agent", "reviewer")
    assert requirement.status == RequirementStatus.BLOCKED
    assert requirement.review_failures == 3


def test_legacy_blocked_plan_retry_rebuilds_missing_workspace_before_development(monkeypatch) -> None:
    session, requirement = session_with_requirement()
    repository = RepositoryConnection(
        project_id=requirement.project_id,
        provider="github",
        external_id="legacy-retry",
        full_name="acme/legacy",
        clone_url="git@github.com:acme/legacy.git",
        web_url="https://github.com/acme/legacy",
    )
    session.add(repository)
    session.flush()
    session.add(
        RequirementRepository(
            requirement_id=requirement.id,
            repository_id=repository.id,
            target_branch="main",
        )
    )
    requirement.status = RequirementStatus.BLOCKED
    requirement.review_failures = 3
    requirement.acceptance_failures = 2
    session.flush()
    settings = Settings(_env_file=None, repository_automation_enabled=True, llm_provider="fake")
    monkeypatch.setattr("app.services.workflow.get_settings", lambda: settings)

    revision = transition_requirement(
        session,
        requirement,
        "retry_planning",
        requirement.version,
        "user",
        requirement.owner_id,
    )
    assert revision is not None and revision.task_type == "agent.revise"
    assert requirement.review_failures == 0
    assert requirement.acceptance_failures == 0

    process_task_result(
        session,
        {
            "task_id": revision.id,
            "status": "completed",
            "output": {"target_architecture": "Revised plan", "risks": []},
        },
    )
    prepare = session.scalars(
        select(WorkflowTask).where(
            WorkflowTask.requirement_id == requirement.id,
            WorkflowTask.task_type == "git.prepare_workspaces",
        )
    ).one()
    assert requirement.status == RequirementStatus.DEVELOPING

    process_task_result(
        session,
        {
            "task_id": prepare.id,
            "status": "completed",
            "output": {
                "workspace_root": "/workspaces/legacy",
                "repositories": [
                    {
                        "repository_id": repository.id,
                        "work_branch": "huaban/req-legacy",
                        "relative_path": repository.id,
                    }
                ],
            },
        },
    )
    dependency = complete_dependency_preparation(session, requirement)
    developer = session.scalars(
        select(WorkflowTask).where(
            WorkflowTask.requirement_id == requirement.id,
            WorkflowTask.task_type == "agent.develop",
        )
    ).one()
    developer_context = json.loads(developer.payload_json)["context"]
    assert developer_context["artifacts"]["workspace_manifest"]["workspace_root"] == "/workspaces/legacy"
    assert dependency.status == "completed"
    assert developer_context["artifacts"]["dependency_manifest"]["network_execution"] is True


def test_legacy_blocked_development_retry_prepares_workspace_when_manifest_is_missing(monkeypatch) -> None:
    session, requirement = session_with_requirement()
    requirement.status = RequirementStatus.BLOCKED
    requirement.review_failures = 3
    settings = Settings(_env_file=None, repository_automation_enabled=True, llm_provider="fake")
    monkeypatch.setattr("app.services.workflow.get_settings", lambda: settings)

    task = transition_requirement(
        session,
        requirement,
        "retry_development",
        requirement.version,
        "user",
        requirement.owner_id,
    )

    assert task is not None and task.task_type == "git.prepare_workspaces"
    assert requirement.review_failures == 0


def test_blocked_acceptance_retry_rebuilds_clean_verification_workspace(monkeypatch) -> None:
    session, requirement = session_with_requirement()
    requirement.status = RequirementStatus.BLOCKED
    requirement.acceptance_failures = 3
    settings = Settings(_env_file=None, repository_automation_enabled=True, llm_provider="fake")
    monkeypatch.setattr("app.services.workflow.get_settings", lambda: settings)

    task = transition_requirement(
        session,
        requirement,
        "retry_acceptance",
        requirement.version,
        "user",
        requirement.owner_id,
    )

    assert task is not None and task.task_type == "git.prepare_verification"
    assert requirement.status == RequirementStatus.ACCEPTING
    assert requirement.acceptance_failures == 0


def test_plan_retry_restores_existing_workspace_then_prepares_dependencies(monkeypatch) -> None:
    session, requirement = session_with_requirement()
    session.add(
        ArtifactVersion(
            requirement_id=requirement.id,
            kind="workspace_manifest",
            version=1,
            content_json=json.dumps(
                {
                    "workspace_root": "/workspaces/existing",
                    "repositories": [],
                }
            ),
            content_markdown="# Workspace",
        )
    )
    requirement.status = RequirementStatus.BLOCKED
    session.flush()
    settings = Settings(_env_file=None, repository_automation_enabled=True, llm_provider="fake")
    monkeypatch.setattr("app.services.workflow.get_settings", lambda: settings)

    revision = transition_requirement(
        session,
        requirement,
        "retry_planning",
        requirement.version,
        "user",
        requirement.owner_id,
    )
    process_task_result(
        session,
        {
            "task_id": revision.id,
            "status": "completed",
            "output": {"target_architecture": "Revised plan", "risks": []},
        },
    )

    restore = session.scalars(
        select(WorkflowTask).where(
            WorkflowTask.requirement_id == requirement.id,
            WorkflowTask.task_type == "git.restore_workspaces",
        )
    ).one()
    assert not session.scalars(
        select(WorkflowTask).where(
            WorkflowTask.requirement_id == requirement.id,
            WorkflowTask.task_type == "agent.develop",
        )
    ).all()

    process_task_result(
        session,
        {
            "task_id": restore.id,
            "status": "completed",
            "output": {
                "workspace_root": "/workspaces/existing",
                "repositories": [],
                "restored": [],
            },
        },
    )
    complete_dependency_preparation(session, requirement)

    developer = session.scalars(
        select(WorkflowTask).where(
            WorkflowTask.requirement_id == requirement.id,
            WorkflowTask.task_type == "agent.develop",
        )
    ).one()
    context = json.loads(developer.payload_json)["context"]
    assert context["artifacts"]["dependency_manifest"]["network_execution"] is True


def test_blocked_development_retry_restores_existing_workspace_before_agent(monkeypatch) -> None:
    session, requirement = session_with_requirement()
    session.add(
        ArtifactVersion(
            requirement_id=requirement.id,
            kind="workspace_manifest",
            version=1,
            content_json=json.dumps(
                {
                    "workspace_root": "/workspaces/existing",
                    "repositories": [],
                }
            ),
            content_markdown="# Workspace",
        )
    )
    requirement.status = RequirementStatus.BLOCKED
    requirement.review_failures = 3
    session.flush()
    settings = Settings(_env_file=None, repository_automation_enabled=True, llm_provider="fake")
    monkeypatch.setattr("app.services.workflow.get_settings", lambda: settings)

    task = transition_requirement(
        session,
        requirement,
        "retry_development",
        requirement.version,
        "user",
        requirement.owner_id,
    )

    assert task is not None and task.task_type == "git.restore_workspaces"
    assert requirement.review_failures == 0
    process_task_result(
        session,
        {
            "task_id": task.id,
            "status": "completed",
            "output": {
                "workspace_root": "/workspaces/existing",
                "repositories": [],
                "restored": [],
            },
        },
    )
    complete_dependency_preparation(session, requirement)
    developer = session.scalars(
        select(WorkflowTask).where(
            WorkflowTask.requirement_id == requirement.id,
            WorkflowTask.task_type == "agent.develop",
        )
    ).one()
    context = json.loads(developer.payload_json)["context"]
    assert context["artifacts"]["workspace_manifest"]["workspace_root"] == "/workspaces/existing"


def test_blocked_development_retry_preserves_changed_files_and_reprepares_dependencies(monkeypatch) -> None:
    session, requirement = session_with_requirement()
    session.add(
        ArtifactVersion(
            requirement_id=requirement.id,
            kind="workspace_manifest",
            version=1,
            content_json=json.dumps(
                {"workspace_root": "/workspaces/existing", "repositories": []}
            ),
            content_markdown="# Workspace",
        )
    )
    failed = WorkflowTask(
        requirement_id=requirement.id,
        task_type="agent.develop",
        status="failed",
        idempotency_key="failed-development",
        payload_json=json.dumps(
            {
                "_failure": {
                    "error_code": "agent.step_budget_exhausted",
                    "error_message": "not ok: expected empty but received undefined",
                    "changed_paths": ["repo-1/value.ts", "repo-1/value.test.ts"],
                }
            }
        ),
    )
    session.add(failed)
    requirement.status = RequirementStatus.BLOCKED
    session.flush()
    settings = Settings(_env_file=None, repository_automation_enabled=True, llm_provider="fake")
    monkeypatch.setattr("app.services.workflow.get_settings", lambda: settings)

    dependency = transition_requirement(
        session,
        requirement,
        "retry_development",
        requirement.version,
        "user",
        requirement.owner_id,
    )

    assert dependency is not None and dependency.task_type == "dependency.prepare"
    dependency_context = json.loads(dependency.payload_json)["context"]
    assert dependency_context["_previous_attempt_failure"]["changed_paths"] == [
        "repo-1/value.ts",
        "repo-1/value.test.ts",
    ]
    process_task_result(
        session,
        {
            "task_id": dependency.id,
            "status": "completed",
            "output": {
                "schema_version": "1.0",
                "scope": "development",
                "network_execution": True,
                "summary": "dependencies ready",
                "repositories": [],
            },
        },
    )
    developer = session.scalars(
        select(WorkflowTask).where(
            WorkflowTask.requirement_id == requirement.id,
            WorkflowTask.task_type == "agent.develop",
            WorkflowTask.status == "queued",
        )
    ).one()
    developer_context = json.loads(developer.payload_json)["context"]
    assert developer_context["_previous_attempt_failure"]["error_message"].startswith("not ok")
    assert developer_context["_previous_attempt_failure"]["changed_paths"] == [
        "repo-1/value.ts",
        "repo-1/value.test.ts",
    ]


def test_invalid_transition_is_rejected() -> None:
    session, requirement = session_with_requirement()
    try:
        transition_requirement(session, requirement, "review_approved", 1, "user", requirement.owner_id)
    except WorkflowError:
        pass
    else:
        raise AssertionError("expected WorkflowError")


def test_closing_requirement_cancels_pending_work_and_ignores_late_result() -> None:
    session, requirement = session_with_requirement()
    task = transition_requirement(
        session,
        requirement,
        "publish",
        requirement.version,
        "user",
        requirement.owner_id,
    )
    assert task is not None
    run = session.get(AgentRun, task.agent_run_id)
    outbox = session.scalar(
        select(OutboxEvent).where(OutboxEvent.aggregate_id == requirement.id)
    )

    try:
        transition_requirement(
            session,
            requirement,
            "cancel",
            requirement.version,
            "user",
            requirement.owner_id,
        )
    except WorkflowError as exc:
        assert str(exc) == "closing reason is required"
    else:
        raise AssertionError("expected closing reason validation")

    transition_requirement(
        session,
        requirement,
        "cancel",
        requirement.version,
        "user",
        requirement.owner_id,
        "业务方向调整，不再继续",
    )

    assert requirement.status == RequirementStatus.CANCELLED
    assert task.status == "cancelled"
    assert run.status == "cancelled"
    assert run.error_code == "requirement.cancelled"
    assert run.completed_at is not None
    assert outbox.published is True

    process_task_result(
        session,
        {
            "task_id": task.id,
            "status": "completed",
            "output": {"summary": "迟到结果", "open_questions": []},
        },
    )
    assert requirement.status == RequirementStatus.CANCELLED
    assert task.status == "cancelled"
    assert not session.scalars(
        select(ArtifactVersion).where(
            ArtifactVersion.requirement_id == requirement.id
        )
    ).all()


def test_paused_and_final_acceptance_requirements_can_be_closed() -> None:
    for state in {RequirementStatus.PAUSED, RequirementStatus.FINAL_ACCEPTANCE}:
        session, requirement = session_with_requirement()
        requirement.status = state
        requirement.paused_from = (
            RequirementStatus.DEVELOPING if state == RequirementStatus.PAUSED else None
        )
        transition_requirement(
            session,
            requirement,
            "cancel",
            requirement.version,
            "user",
            requirement.owner_id,
            "主动结束",
        )
        assert requirement.status == RequirementStatus.CANCELLED
        assert requirement.paused_from is None


def test_requirement_cannot_be_closed_while_remote_merge_is_running() -> None:
    session, requirement = session_with_requirement()
    requirement.status = RequirementStatus.MERGING
    try:
        transition_requirement(
            session,
            requirement,
            "cancel",
            requirement.version,
            "user",
            requirement.owner_id,
            "停止需求",
        )
    except WorkflowError:
        pass
    else:
        raise AssertionError("expected active remote merge to reject closing")


def test_dependency_failure_blocks_before_developer_starts(monkeypatch) -> None:
    session, requirement = session_with_requirement()
    requirement.status = RequirementStatus.DEVELOPING
    session.add(
        ArtifactVersion(
            requirement_id=requirement.id,
            kind="workspace_manifest",
            version=1,
            content_json=json.dumps(
                {"workspace_root": "/workspaces/req", "repositories": []}
            ),
            content_markdown="# Workspace",
        )
    )
    session.flush()
    settings = Settings(
        _env_file=None,
        repository_automation_enabled=True,
        task_max_attempts=1,
        llm_provider="fake",
    )
    monkeypatch.setattr("app.services.workflow.get_settings", lambda: settings)
    monkeypatch.setattr("app.services.task_results.get_settings", lambda: settings)

    dependency = transition_requirement(
        session,
        requirement,
        "workspace_ready",
        requirement.version,
        "git_worker",
        None,
    )
    assert dependency is not None and dependency.task_type == "dependency.prepare"

    process_task_result(
        session,
        {
            "task_id": dependency.id,
            "status": "failed",
            "error_code": "dependency.install_failed",
            "error_message": "npm dependency installation failed",
            "retryable": False,
        },
    )

    assert requirement.status == RequirementStatus.BLOCKED
    latest = session.scalars(
        select(WorkflowTransition)
        .where(WorkflowTransition.requirement_id == requirement.id)
        .order_by(WorkflowTransition.created_at.desc())
    ).first()
    assert latest.actor_type == "dependency_worker"
    assert latest.event == "dependency_failed"
    assert not session.scalars(
        select(WorkflowTask).where(
            WorkflowTask.requirement_id == requirement.id,
            WorkflowTask.task_type == "agent.develop",
        )
    ).all()


def test_review_rejection_restores_workspace_and_prepares_dependencies(monkeypatch) -> None:
    session, requirement = session_with_requirement()
    requirement.status = RequirementStatus.REVIEWING
    session.add(
        ArtifactVersion(
            requirement_id=requirement.id,
            kind="workspace_manifest",
            version=1,
            content_json=json.dumps(
                {"workspace_root": "/workspaces/req", "repositories": []}
            ),
            content_markdown="# Workspace",
        )
    )
    session.flush()
    settings = Settings(
        _env_file=None,
        repository_automation_enabled=True,
        llm_provider="fake",
    )
    monkeypatch.setattr("app.services.workflow.get_settings", lambda: settings)

    restore = transition_requirement(
        session,
        requirement,
        "review_rejected",
        requirement.version,
        "agent",
        "reviewer",
        "package validation is missing",
    )

    assert restore is not None and restore.task_type == "git.restore_workspaces"
    assert not session.scalars(
        select(WorkflowTask).where(
            WorkflowTask.requirement_id == requirement.id,
            WorkflowTask.task_type == "agent.develop",
        )
    ).all()

    process_task_result(
        session,
        {
            "task_id": restore.id,
            "status": "completed",
            "output": {
                "workspace_root": "/workspaces/req",
                "repositories": [],
                "restored": [],
            },
        },
    )

    dependency = session.scalars(
        select(WorkflowTask).where(
            WorkflowTask.requirement_id == requirement.id,
            WorkflowTask.task_type == "dependency.prepare",
        )
    ).one()
    assert dependency is not None


def test_validation_only_rework_restores_generated_files_before_review(monkeypatch) -> None:
    session, requirement = session_with_requirement()
    requirement.status = RequirementStatus.DEVELOPING
    for kind, content in (
        ("workspace_manifest", {"workspace_root": "/workspaces/req", "repositories": []}),
        ("development_commit_manifest", {"repositories": [{"repository_id": "repo-1"}]}),
    ):
        session.add(
            ArtifactVersion(
                requirement_id=requirement.id,
                kind=kind,
                version=1,
                content_json=json.dumps(content),
                content_markdown=f"# {kind}",
            )
        )
    session.flush()
    settings = Settings(
        _env_file=None,
        repository_automation_enabled=True,
        llm_provider="fake",
    )
    monkeypatch.setattr("app.services.workflow.get_settings", lambda: settings)

    developer = transition_requirement(
        session,
        requirement,
        "dependencies_ready",
        requirement.version,
        "dependency_worker",
        None,
    )
    process_task_result(
        session,
        {
            "task_id": developer.id,
            "status": "completed",
            "output": {
                "summary": "Package command passed",
                "repositories_changed": [],
                "commits": {},
                "tests": [{"command": ["npm", "run", "package:cli"], "status": "passed"}],
                "unresolved_risks": [],
            },
        },
    )

    restore = session.scalars(
        select(WorkflowTask).where(
            WorkflowTask.requirement_id == requirement.id,
            WorkflowTask.task_type == "git.restore_validation_workspace",
        )
    ).one()
    assert requirement.status == RequirementStatus.DEVELOPING
    process_task_result(
        session,
        {
            "task_id": restore.id,
            "status": "completed",
            "output": {
                "workspace_root": "/workspaces/req",
                "repositories": [],
                "restored": [],
            },
        },
    )

    review = session.scalars(
        select(WorkflowTask).where(
            WorkflowTask.requirement_id == requirement.id,
            WorkflowTask.task_type == "agent.review",
        )
    ).one()
    assert review is not None
    assert requirement.status == RequirementStatus.REVIEWING


def test_downstream_agent_receives_versioned_artifacts_and_repository_context() -> None:
    session, requirement = session_with_requirement()
    repository = RepositoryConnection(project_id=requirement.project_id, provider="github", external_id="1", full_name="acme/api", clone_url="https://github.com/acme/api.git", web_url="https://github.com/acme/api")
    session.add(repository)
    session.flush()
    session.add(RequirementRepository(requirement_id=requirement.id, repository_id=repository.id, target_branch="main"))
    session.add(ArtifactVersion(requirement_id=requirement.id, kind="clarification_spec", version=1, content_json=json.dumps({"summary": "confirmed"}), content_markdown="# confirmed"))
    requirement.status = RequirementStatus.AWAITING_PLAN
    session.flush()
    task = transition_requirement(session, requirement, "confirm_plan", requirement.version, "user", requirement.owner_id)
    context = json.loads(task.payload_json)["context"]
    assert context["repositories"][0]["full_name"] == "acme/api"
    assert context["artifacts"]["clarification_spec"]["summary"] == "confirmed"


def test_architect_receives_the_owners_plan_change_feedback() -> None:
    session, requirement = session_with_requirement()
    requirement.status = RequirementStatus.AWAITING_PLAN
    session.add(
        ArtifactVersion(
            requirement_id=requirement.id,
            kind="architecture_plan",
            version=1,
            content_json=json.dumps({"target_architecture": "直接修改主分支"}),
            content_markdown="# plan",
        )
    )
    session.flush()

    task = transition_requirement(
        session,
        requirement,
        "request_plan_change",
        requirement.version,
        "user",
        requirement.owner_id,
        "禁止直接修改 main；必须使用工作分支并通过 PR 合并。",
    )

    context = json.loads(task.payload_json)["context"]
    messages = session.scalars(
        select(ConversationMessage).where(ConversationMessage.requirement_id == requirement.id)
    ).all()
    assert requirement.status == RequirementStatus.PLANNING
    assert len(messages) == 1
    assert context["conversation"][-1]["body"] == "禁止直接修改 main；必须使用工作分支并通过 PR 合并。"
    assert context["artifacts"]["architecture_plan"]["target_architecture"] == "直接修改主分支"


def test_plan_change_requires_actionable_feedback() -> None:
    session, requirement = session_with_requirement()
    requirement.status = RequirementStatus.AWAITING_PLAN

    try:
        transition_requirement(
            session,
            requirement,
            "request_plan_change",
            requirement.version,
            "user",
            requirement.owner_id,
            "   ",
        )
    except WorkflowError as error:
        assert "plan feedback" in str(error)
    else:
        raise AssertionError("expected blank plan feedback to be rejected")


def test_clarifier_receives_the_requesters_answer_on_the_next_round() -> None:
    session, requirement = session_with_requirement()
    requirement.status = RequirementStatus.AWAITING_CLARIFICATION
    session.add(
        ArtifactVersion(
            requirement_id=requirement.id,
            kind="clarification_spec",
            version=1,
            content_json=json.dumps({"summary": "needs details", "open_questions": ["Which files?"]}),
            content_markdown="# needs details",
        )
    )
    session.flush()

    task = transition_requirement(
        session,
        requirement,
        "request_more_clarification",
        requirement.version,
        "user",
        requirement.owner_id,
        "请整理根目录和 docs 目录中的全部 AGENTS.md，并保留原有约束。",
    )

    context = json.loads(task.payload_json)["context"]
    messages = session.scalars(
        select(ConversationMessage).where(ConversationMessage.requirement_id == requirement.id)
    ).all()
    assert requirement.status == RequirementStatus.CLARIFYING
    assert len(messages) == 1
    assert context["conversation"][-1]["body"] == "请整理根目录和 docs 目录中的全部 AGENTS.md，并保留原有约束。"
    assert context["artifacts"]["clarification_spec"]["open_questions"] == ["Which files?"]


def test_open_questions_block_premature_clarification_confirmation() -> None:
    session, requirement = session_with_requirement()
    requirement.status = RequirementStatus.AWAITING_CLARIFICATION
    session.add(
        ArtifactVersion(
            requirement_id=requirement.id,
            kind="clarification_spec",
            version=1,
            content_json=json.dumps({"summary": "needs details", "open_questions": ["Which files?"]}),
            content_markdown="# needs details",
        )
    )
    session.flush()

    try:
        transition_requirement(
            session,
            requirement,
            "confirm_clarification",
            requirement.version,
            "user",
            requirement.owner_id,
        )
    except WorkflowError as error:
        assert "open questions" in str(error)
    else:
        raise AssertionError("expected open questions to block confirmation")


def test_repository_automation_routes_local_commit_review_then_publish(monkeypatch) -> None:
    session, requirement = session_with_requirement()
    repository = RepositoryConnection(
        project_id=requirement.project_id,
        provider="github",
        external_id="2",
        full_name="acme/worker",
        clone_url="https://github.com/acme/worker.git",
        web_url="https://github.com/acme/worker",
    )
    session.add(repository)
    session.flush()
    link = RequirementRepository(
        requirement_id=requirement.id,
        repository_id=repository.id,
        target_branch="main",
    )
    session.add(link)
    session.add(
        ArtifactVersion(
            requirement_id=requirement.id,
            kind="architecture_plan",
            version=1,
            content_json=json.dumps({"target_architecture": "Implement safely"}),
            content_markdown="# Plan",
        )
    )
    requirement.status = RequirementStatus.AWAITING_PLAN
    session.flush()
    settings = Settings(_env_file=None, repository_automation_enabled=True, llm_provider="fake")
    monkeypatch.setattr("app.services.workflow.get_settings", lambda: settings)

    prepare_task = transition_requirement(
        session, requirement, "confirm_plan", requirement.version, "user", requirement.owner_id
    )
    assert prepare_task is not None and prepare_task.task_type == "git.prepare_workspaces"
    process_task_result(
        session,
        {
            "task_id": prepare_task.id,
            "status": "completed",
            "output": {
                "workspace_root": "/workspaces/req",
                "repositories": [{"repository_id": repository.id, "work_branch": "forgeflow/req-1"}],
            },
        },
    )
    dependency_task = session.scalars(
        select(WorkflowTask).where(
            WorkflowTask.requirement_id == requirement.id,
            WorkflowTask.task_type == "dependency.prepare",
        )
    ).one()
    assert not session.scalars(
        select(WorkflowTask).where(
            WorkflowTask.requirement_id == requirement.id,
            WorkflowTask.task_type == "agent.develop",
        )
    ).all()
    complete_dependency_preparation(session, requirement)
    develop_task = session.scalars(
        select(WorkflowTask).where(WorkflowTask.requirement_id == requirement.id, WorkflowTask.task_type == "agent.develop")
    ).one()
    assert dependency_task.status == "completed"
    assert requirement.status == RequirementStatus.DEVELOPING

    process_task_result(
        session,
        {
            "task_id": develop_task.id,
            "status": "completed",
            "output": {
                "schema_version": "1.0",
                "summary": "Implemented and tested",
                "repositories_changed": [repository.id],
                "commits": {},
                "tests": [{"command": ["pytest"], "status": "passed"}],
                "unresolved_risks": [],
            },
        },
    )
    commit_task = session.scalars(
        select(WorkflowTask).where(WorkflowTask.requirement_id == requirement.id, WorkflowTask.task_type == "git.commit_changes")
    ).one()
    assert requirement.status == RequirementStatus.DEVELOPING
    session.add(
        ArtifactVersion(
            requirement_id=requirement.id,
            kind="code_review_report",
            version=1,
            content_json=json.dumps(
                {"approved": False, "summary": "stale rejection", "findings": [{"severity": "blocker"}]}
            ),
            content_markdown="stale rejection",
        )
    )
    session.flush()

    process_task_result(
        session,
        {
            "task_id": commit_task.id,
            "status": "completed",
            "output": {
                "summary": "Committed locally",
                "push_performed": False,
                "combined_diff": "diff --git a/value.txt b/value.txt",
                "repositories": [{
                    "requirement_repository_id": link.id,
                    "repository_id": repository.id,
                    "full_name": "acme/worker",
                    "work_branch": "forgeflow/req-1",
                    "head_sha": "abc123",
                    "baseline_sha": "base123",
                    "diff_sha256": "digest",
                    "changed_files": [{"path": "value.txt", "content": "after\n", "sha256": "digest", "size_bytes": 6}],
                }],
            },
        },
    )
    review_task = session.scalars(
        select(WorkflowTask).where(WorkflowTask.requirement_id == requirement.id, WorkflowTask.task_type == "agent.review")
    ).one()
    review_context = json.loads(review_task.payload_json)["context"]
    assert requirement.status == RequirementStatus.REVIEWING
    assert session.get(AgentRun, review_task.agent_run_id).agent_key == "agent2"
    assert link.status == "committed"
    assert link.work_branch == "forgeflow/req-1"
    assert link.head_sha == "abc123"
    assert link.pull_request_number is None
    assert review_context["repositories"][0]["work_branch"] == "forgeflow/req-1"
    assert review_context["repositories"][0]["head_sha"] == "abc123"
    assert review_context["artifacts"]["development_commit_manifest"]["combined_diff"].startswith("diff --git")
    assert review_context["artifacts"]["development_commit_manifest"]["repositories"][0]["changed_files"][0]["content"] == "after\n"
    assert "code_review_report" not in review_context["artifacts"]
    assert review_context["review_stage_policy"]["stage"] == "pre_publish"
    assert review_context["review_stage_policy"]["missing_push_is_not_a_review_finding"] is True
    process_task_result(
        session,
        {"task_id": review_task.id, "status": "completed", "output": {"approved": True, "summary": "评审通过", "findings": []}},
    )
    publish_task = session.scalars(
        select(WorkflowTask).where(WorkflowTask.requirement_id == requirement.id, WorkflowTask.task_type == "git.publish_changes")
    ).one()
    assert requirement.status == RequirementStatus.REVIEWING
    publish_context = json.loads(publish_task.payload_json)["context"]
    assert publish_context["artifacts"]["development_commit_manifest"]["repositories"][0]["head_sha"] == "abc123"
    process_task_result(
        session,
        {
            "task_id": publish_task.id,
            "status": "completed",
            "output": {
                "summary": "Published reviewed commit",
                "combined_diff": "diff --git a/value.txt b/value.txt",
                "repositories": [{
                    "requirement_repository_id": link.id,
                    "repository_id": repository.id,
                    "work_branch": "forgeflow/req-1",
                    "pull_request_number": 17,
                    "pull_request_url": "https://example.invalid/pr/17",
                    "head_sha": "abc123",
                }],
            },
        },
    )
    verification_task = session.scalars(
        select(WorkflowTask).where(WorkflowTask.requirement_id == requirement.id, WorkflowTask.task_type == "git.prepare_verification")
    ).one()
    assert requirement.status == RequirementStatus.ACCEPTING
    assert link.pull_request_number == 17
    process_task_result(
        session,
        {
            "task_id": verification_task.id,
            "status": "completed",
            "output": {"workspace_root": "/workspaces/req-verify", "checkout_type": "published_heads", "repositories": []},
        },
    )
    complete_scoped_dependency_preparation(
        session,
        requirement,
        "dependency.prepare_verification",
        "acceptance",
    )
    acceptance_task = session.scalars(
        select(WorkflowTask).where(WorkflowTask.requirement_id == requirement.id, WorkflowTask.task_type == "agent.accept")
    ).one()
    acceptance_context = json.loads(acceptance_task.payload_json)["context"]
    assert acceptance_context["artifacts"]["verification_manifest"]["checkout_type"] == "published_heads"


def test_repository_automation_prepares_real_analysis_before_architect(monkeypatch) -> None:
    session, requirement = session_with_requirement()
    repository = RepositoryConnection(
        project_id=requirement.project_id,
        provider="github",
        external_id="analysis-1",
        full_name="acme/analyzed",
        clone_url="https://github.com/acme/analyzed.git",
        web_url="https://github.com/acme/analyzed",
    )
    session.add(repository)
    session.flush()
    session.add(RequirementRepository(requirement_id=requirement.id, repository_id=repository.id, target_branch="main"))
    session.add(
        ArtifactVersion(
            requirement_id=requirement.id,
            kind="clarification_spec",
            version=1,
            content_json=json.dumps({"summary": "confirmed"}),
            content_markdown="# confirmed",
        )
    )
    requirement.status = RequirementStatus.AWAITING_CLARIFICATION
    session.flush()
    settings = Settings(_env_file=None, repository_automation_enabled=True, llm_provider="fake")
    monkeypatch.setattr("app.services.workflow.get_settings", lambda: settings)
    analysis_task = transition_requirement(
        session, requirement, "confirm_clarification", requirement.version, "user", requirement.owner_id
    )
    assert analysis_task.task_type == "git.prepare_analysis"
    process_task_result(
        session,
        {
            "task_id": analysis_task.id,
            "status": "completed",
            "output": {
                "source": "trusted_read_only_checkout",
                "repositories": [{"repository_id": repository.id, "head_sha": "abc", "file_tree": ["README.md"]}],
            },
        },
    )
    architect_task = session.scalars(
        select(WorkflowTask).where(WorkflowTask.requirement_id == requirement.id, WorkflowTask.task_type == "agent.architect")
    ).one()
    architect_context = json.loads(architect_task.payload_json)["context"]
    assert requirement.status == RequirementStatus.PLANNING
    assert architect_context["artifacts"]["repository_analysis"]["repositories"][0]["file_tree"] == ["README.md"]


def test_retryable_worker_failure_gets_new_task_before_requirement_blocks(monkeypatch) -> None:
    session, requirement = session_with_requirement()
    settings = Settings(_env_file=None, task_max_attempts=2, task_retry_base_seconds=0, llm_provider="fake")
    monkeypatch.setattr("app.services.task_results.get_settings", lambda: settings)
    monkeypatch.setattr("app.services.workflow.get_settings", lambda: settings)
    first = transition_requirement(session, requirement, "publish", requirement.version, "user", requirement.owner_id)
    assert first is not None

    process_task_result(
        session,
        {"task_id": first.id, "status": "failed", "error_code": "model.timeout", "retryable": True},
    )
    tasks = session.scalars(
        select(WorkflowTask).where(WorkflowTask.requirement_id == requirement.id).order_by(WorkflowTask.created_at)
    ).all()
    assert len(tasks) == 2
    assert tasks[-1].status == "queued"
    assert tasks[-1].id != first.id
    retry_payload = json.loads(tasks[-1].payload_json)
    assert retry_payload["context"]["_previous_attempt_failure"] == {
        "error_code": "model.timeout",
        "error_message": "",
        "changed_paths": [],
    }
    assert requirement.status == RequirementStatus.CLARIFYING
    process_task_result(
        session,
        {"task_id": first.id, "status": "failed", "error_code": "model.timeout", "retryable": True},
    )
    assert len(session.scalars(select(WorkflowTask).where(WorkflowTask.requirement_id == requirement.id)).all()) == 2

    process_task_result(
        session,
        {"task_id": tasks[-1].id, "status": "failed", "error_code": "model.timeout", "retryable": True},
    )
    assert requirement.status == RequirementStatus.BLOCKED


def test_terminal_agent_failure_persists_diagnostic_reason(monkeypatch) -> None:
    session, requirement = session_with_requirement()
    settings = Settings(_env_file=None, task_max_attempts=1, llm_provider="fake")
    monkeypatch.setattr("app.services.task_results.get_settings", lambda: settings)
    monkeypatch.setattr("app.services.workflow.get_settings", lambda: settings)
    task = transition_requirement(session, requirement, "publish", requirement.version, "user", requirement.owner_id)

    process_task_result(
        session,
        {
            "task_id": task.id,
            "status": "failed",
            "error_code": "agent.step_budget_exhausted",
            "error_message": "developer tool loop exhausted after repeated reads",
            "retryable": False,
            "token_usage": 321,
            "model": "deepseek-v4-flash",
        },
    )

    run = session.get(AgentRun, task.agent_run_id)
    assert run.error_message == "agent.step_budget_exhausted: developer tool loop exhausted after repeated reads"
    assert run.token_usage == 321
    assert run.model == "deepseek-v4-flash"
    transition = session.scalars(
        select(WorkflowTransition)
        .where(WorkflowTransition.requirement_id == requirement.id)
        .order_by(WorkflowTransition.created_at.desc())
    ).first()
    assert transition.reason == "agent.step_budget_exhausted: developer tool loop exhausted after repeated reads"


def test_review_rework_and_acceptance_replanning_feed_evidence_back_to_developer(monkeypatch) -> None:
    session, requirement = session_with_requirement()
    settings = Settings(_env_file=None, repository_automation_enabled=False, llm_provider="fake")
    monkeypatch.setattr("app.services.workflow.get_settings", lambda: settings)
    requirement.status = RequirementStatus.DEVELOPING
    review = transition_requirement(
        session, requirement, "development_ready", requirement.version, "agent", "developer"
    )
    process_task_result(
        session,
        {"task_id": review.id, "status": "completed", "output": {"approved": False, "summary": "缺少边界测试", "findings": [{"severity": "high"}]}},
    )
    developer = session.scalars(
        select(WorkflowTask).where(WorkflowTask.requirement_id == requirement.id, WorkflowTask.task_type == "agent.develop").order_by(WorkflowTask.created_at)
    ).all()[-1]
    developer_context = json.loads(developer.payload_json)["context"]
    assert developer_context["artifacts"]["code_review_report"]["summary"] == "缺少边界测试"
    assert developer_context["artifacts"]["code_review_report"]["findings"] == [{"severity": "high"}]

    process_task_result(
        session,
        {"task_id": developer.id, "status": "completed", "output": {"summary": "补齐测试", "repositories_changed": ["repo"], "commits": {}, "tests": [{"status": "passed"}], "unresolved_risks": []}},
    )
    approved_review = session.scalars(
        select(WorkflowTask).where(WorkflowTask.requirement_id == requirement.id, WorkflowTask.task_type == "agent.review").order_by(WorkflowTask.created_at)
    ).all()[-1]
    process_task_result(
        session,
        {"task_id": approved_review.id, "status": "completed", "output": {"approved": True, "summary": "评审通过", "findings": []}},
    )
    acceptance = session.scalars(
        select(WorkflowTask).where(WorkflowTask.requirement_id == requirement.id, WorkflowTask.task_type == "agent.accept")
    ).one()
    process_task_result(
        session,
        {"task_id": acceptance.id, "status": "completed", "output": {"approved": False, "summary": "验收场景失败", "criteria": []}},
    )
    revision = session.scalars(
        select(WorkflowTask).where(WorkflowTask.requirement_id == requirement.id, WorkflowTask.task_type == "agent.revise")
    ).one()
    process_task_result(
        session,
        {"task_id": revision.id, "status": "completed", "output": {"target_architecture": "调整实现方案", "risks": []}},
    )
    revised_developer = session.scalars(
        select(WorkflowTask).where(WorkflowTask.requirement_id == requirement.id, WorkflowTask.task_type == "agent.develop").order_by(WorkflowTask.created_at)
    ).all()[-1]
    revised_context = json.loads(revised_developer.payload_json)["context"]
    assert requirement.status == RequirementStatus.DEVELOPING
    assert revised_context["artifacts"]["acceptance_report"]["summary"] == "验收场景失败"
    assert revised_context["artifacts"]["architecture_revision"]["target_architecture"] == "调整实现方案"


def test_two_repositories_merge_in_order_then_final_acceptance_completes(monkeypatch) -> None:
    session, requirement = session_with_requirement()
    settings = Settings(_env_file=None, llm_provider="fake", repository_automation_enabled=True)
    monkeypatch.setattr("app.services.workflow.get_settings", lambda: settings)
    session.add(
        ArtifactVersion(
            requirement_id=requirement.id,
            kind="clarification_spec",
            version=1,
            content_json=json.dumps(
                {
                    "acceptance_criteria": [
                        {
                            "id": "AC-1",
                            "description": "Combined system remains healthy",
                            "verification_method": "Run an independent combination check",
                            "priority": "must",
                        }
                    ]
                }
            ),
            content_markdown="# Acceptance criteria",
        )
    )

    def supported_acceptance(summary: str) -> dict:
        return {
            "approved": True,
            "summary": summary,
            "criteria": [
                {
                    "criterion_id": "AC-1",
                    "status": "passed",
                    "summary": "Combination check passed",
                    "evidence_paths": ["agent4-test-1"],
                }
            ],
            "regression_results": [
                {
                    "evidence_id": "agent4-test-1",
                    "source": "agent4_independent",
                    "type": "command",
                    "status": "passed",
                    "criterion_ids": ["AC-1"],
                }
            ],
            "environment": {"workspace": "clean_sha_verified_checkout"},
        }

    links = []
    for order, name in enumerate(["acme/api", "acme/web"]):
        repository = RepositoryConnection(
            project_id=requirement.project_id,
            provider="github",
            external_id=f"merge-{order}",
            full_name=name,
            clone_url=f"https://github.com/{name}.git",
            web_url=f"https://github.com/{name}",
        )
        session.add(repository)
        session.flush()
        link = RequirementRepository(
            requirement_id=requirement.id,
            repository_id=repository.id,
            target_branch="main",
            work_branch="forgeflow/req",
            pull_request_number=order + 10,
            head_sha=f"head{order}",
            merge_order=order,
            status="ready",
        )
        session.add(link)
        session.flush()
        links.append((link, repository))
    requirement.status = RequirementStatus.AWAITING_MERGE

    for index, (link, repository) in enumerate(links):
        attempt = MergeAttempt(
            requirement_id=requirement.id,
            requirement_repository_id=link.id,
            expected_head_sha=link.head_sha,
            actor_id=requirement.owner_id,
        )
        session.add(attempt)
        session.flush()
        task = transition_requirement(
            session,
            requirement,
            "begin_merge",
            requirement.version,
            "user",
            requirement.owner_id,
            task_context={
                "merge_attempt_id": attempt.id,
                "requirement_repository_id": link.id,
                "provider": repository.provider,
                "repository": repository.full_name,
                "pull_request_number": link.pull_request_number,
                "head_sha": link.head_sha,
            },
        )
        process_task_result(
            session,
            {"task_id": task.id, "status": "completed", "output": {"merged_sha": f"merged{index}", "checks": [{"conclusion": "success"}]}},
        )
        assert link.status == "merged"
        if index == 0:
            assert requirement.status == RequirementStatus.REGRESSION
            incremental_verification = session.scalars(
                select(WorkflowTask).where(
                    WorkflowTask.requirement_id == requirement.id,
                    WorkflowTask.task_type == "git.prepare_incremental_verification",
                )
            ).one()
            process_task_result(
                session,
                {
                    "task_id": incremental_verification.id,
                    "status": "completed",
                    "output": {
                        "workspace_root": "/workspaces/incremental",
                        "checkout_type": "incremental_combination",
                        "repositories": [],
                    },
                },
            )
            complete_scoped_dependency_preparation(
                session,
                requirement,
                "dependency.prepare_incremental_verification",
                "regression",
            )
            regression = session.scalars(
                select(WorkflowTask).where(
                    WorkflowTask.requirement_id == requirement.id,
                    WorkflowTask.task_type == "agent.regression",
                )
            ).one()
            assert session.get(AgentRun, regression.agent_run_id).agent_key == "agent4"
            process_task_result(
                session,
                {
                    "task_id": regression.id,
                    "status": "completed",
                    "output": supported_acceptance("逐仓组合回归通过"),
                },
            )
            assert requirement.status == RequirementStatus.AWAITING_MERGE

    final_verification_task = session.scalars(
        select(WorkflowTask).where(WorkflowTask.requirement_id == requirement.id, WorkflowTask.task_type == "git.prepare_final_verification")
    ).one()
    assert requirement.status == RequirementStatus.FINAL_ACCEPTANCE
    process_task_result(
        session,
        {
            "task_id": final_verification_task.id,
            "status": "completed",
            "output": {"workspace_root": "/workspaces/final", "checkout_type": "merged_targets", "repositories": []},
        },
    )
    complete_scoped_dependency_preparation(
        session,
        requirement,
        "dependency.prepare_final_verification",
        "final_acceptance",
    )
    final_task = session.scalars(
        select(WorkflowTask).where(WorkflowTask.requirement_id == requirement.id, WorkflowTask.task_type == "agent.final_accept")
    ).one()
    assert requirement.status == RequirementStatus.FINAL_ACCEPTANCE
    process_task_result(
        session,
        {
            "task_id": final_task.id,
            "status": "completed",
            "output": supported_acceptance("组合验收通过"),
        },
    )
    assert requirement.status == RequirementStatus.COMPLETED


def test_final_acceptance_rejection_blocks_requirement(monkeypatch) -> None:
    session, requirement = session_with_requirement()
    settings = Settings(_env_file=None, llm_provider="fake", repository_automation_enabled=False)
    monkeypatch.setattr("app.services.workflow.get_settings", lambda: settings)
    monkeypatch.setattr("app.services.task_results.get_settings", lambda: settings)
    requirement.status = RequirementStatus.MERGING
    final_task = transition_requirement(
        session, requirement, "all_repositories_merged", requirement.version, "git_worker", None
    )
    process_task_result(
        session,
        {"task_id": final_task.id, "status": "completed", "output": {"approved": False, "summary": "组合回归失败", "criteria": []}},
    )
    assert requirement.status == RequirementStatus.BLOCKED


def test_incremental_regression_failure_blocks_next_repository_merge(monkeypatch) -> None:
    session, requirement = session_with_requirement()
    settings = Settings(_env_file=None, llm_provider="fake", repository_automation_enabled=True)
    monkeypatch.setattr("app.services.workflow.get_settings", lambda: settings)
    requirement.status = RequirementStatus.REGRESSION
    task = transition_requirement(
        session,
        requirement,
        "incremental_verification_ready",
        requirement.version,
        "git_worker",
        None,
    )
    process_task_result(
        session,
        {
            "task_id": task.id,
            "status": "completed",
            "output": {
                "schema_version": "1.0",
                "scope": "regression",
                "network_execution": True,
                "summary": "依赖准备完成",
                "repositories": [],
            },
        },
    )
    regression = session.scalars(
        select(WorkflowTask).where(
            WorkflowTask.requirement_id == requirement.id,
            WorkflowTask.task_type == "agent.regression",
        )
    ).one()
    process_task_result(
        session,
        {
            "task_id": regression.id,
            "status": "completed",
            "output": {"approved": False, "summary": "跨仓接口回归失败", "criteria": []},
        },
    )
    assert requirement.status == RequirementStatus.BLOCKED
