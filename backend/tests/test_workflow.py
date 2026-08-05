from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.database import Base
import json

from app.core.config import Settings
from app.models.entities import AgentRun, ArtifactVersion, MergeAttempt, Project, RepositoryConnection, Requirement, RequirementRepository, RequirementStatus, User, WorkflowTask
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


def test_happy_path_schedules_clarifier() -> None:
    session, requirement = session_with_requirement()
    task = transition_requirement(session, requirement, "publish", 1, "user", requirement.owner_id)
    assert requirement.status == RequirementStatus.CLARIFYING
    assert requirement.version == 2
    assert task is not None and task.task_type == "agent.clarify"
    assert session.get(AgentRun, task.agent_run_id).agent_key == "agent1"


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


def test_invalid_transition_is_rejected() -> None:
    session, requirement = session_with_requirement()
    try:
        transition_requirement(session, requirement, "review_approved", 1, "user", requirement.owner_id)
    except WorkflowError:
        pass
    else:
        raise AssertionError("expected WorkflowError")


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


def test_repository_automation_routes_prepare_develop_publish_and_review(monkeypatch) -> None:
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
    develop_task = session.scalars(
        select(WorkflowTask).where(WorkflowTask.requirement_id == requirement.id, WorkflowTask.task_type == "agent.develop")
    ).one()
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
    publish_task = session.scalars(
        select(WorkflowTask).where(WorkflowTask.requirement_id == requirement.id, WorkflowTask.task_type == "git.publish_changes")
    ).one()
    assert requirement.status == RequirementStatus.DEVELOPING

    process_task_result(
        session,
        {
            "task_id": publish_task.id,
            "status": "completed",
            "output": {
                "summary": "Published",
                "combined_diff": "diff --git a/value.txt b/value.txt",
                "repositories": [{
                    "requirement_repository_id": link.id,
                    "work_branch": "forgeflow/req-1",
                    "pull_request_number": 17,
                    "head_sha": "abc123",
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
    assert link.pull_request_number == 17
    assert review_context["artifacts"]["delivery_manifest"]["combined_diff"].startswith("diff --git")
    process_task_result(
        session,
        {"task_id": review_task.id, "status": "completed", "output": {"approved": True, "summary": "评审通过", "findings": []}},
    )
    verification_task = session.scalars(
        select(WorkflowTask).where(WorkflowTask.requirement_id == requirement.id, WorkflowTask.task_type == "git.prepare_verification")
    ).one()
    assert requirement.status == RequirementStatus.ACCEPTING
    process_task_result(
        session,
        {
            "task_id": verification_task.id,
            "status": "completed",
            "output": {"workspace_root": "/workspaces/req-verify", "checkout_type": "published_heads", "repositories": []},
        },
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
                    "output": {"approved": True, "summary": "逐仓组合回归通过", "criteria": []},
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
    final_task = session.scalars(
        select(WorkflowTask).where(WorkflowTask.requirement_id == requirement.id, WorkflowTask.task_type == "agent.final_accept")
    ).one()
    assert requirement.status == RequirementStatus.FINAL_ACCEPTANCE
    process_task_result(
        session,
        {"task_id": final_task.id, "status": "completed", "output": {"approved": True, "summary": "组合验收通过", "criteria": []}},
    )
    assert requirement.status == RequirementStatus.COMPLETED


def test_final_acceptance_rejection_blocks_requirement(monkeypatch) -> None:
    session, requirement = session_with_requirement()
    settings = Settings(_env_file=None, llm_provider="fake")
    monkeypatch.setattr("app.services.workflow.get_settings", lambda: settings)
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
            "output": {"approved": False, "summary": "跨仓接口回归失败", "criteria": []},
        },
    )
    assert requirement.status == RequirementStatus.BLOCKED
