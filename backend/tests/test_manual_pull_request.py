from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import Base
from app.main import (
    create_requirement_pull_request,
    get_requirement_task,
    transition,
    update_requirement_repository_delivery,
)
from app.models.entities import (
    MergeAttempt,
    Project,
    ProjectMember,
    ProjectRole,
    RepositoryConnection,
    Requirement,
    RequirementRepository,
    RequirementStatus,
    User,
    WorkflowTask,
)
from app.schemas.domain import RequirementRepositoryDeliveryUpdate, TransitionRequest
from app.services.provider_secrets import ProviderSecretStore
from app.services.task_results import process_task_result


def test_owner_registers_pull_request_for_reviewed_head(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = Session(engine)
    owner = User(email="owner@example.com", display_name="Owner", password_hash="x")
    session.add(owner)
    session.flush()
    project = Project(key="PR", name="PR Gate", owner_id=owner.id)
    session.add(project)
    session.flush()
    session.add(ProjectMember(project_id=project.id, user_id=owner.id, role=ProjectRole.OWNER))
    repository = RepositoryConnection(
        project_id=project.id,
        provider="github",
        external_id="1",
        full_name="acme/service",
        clone_url="git@github.com:acme/service.git",
        web_url="https://github.com/acme/service",
    )
    session.add(repository)
    session.flush()
    requirement = Requirement(
        project_id=project.id,
        number=1,
        title="Feature",
        description="A sufficiently detailed feature",
        owner_id=owner.id,
        status=RequirementStatus.AWAITING_MERGE,
    )
    session.add(requirement)
    session.flush()
    link = RequirementRepository(
        requirement_id=requirement.id,
        repository_id=repository.id,
        target_branch="main",
        work_branch="huaban/req-1",
        head_sha="abcdef1234567890",
        status="ready",
    )
    session.add(link)
    session.commit()

    updated = update_requirement_repository_delivery(
        requirement.id,
        link.id,
        RequirementRepositoryDeliveryUpdate(
            work_branch="huaban/req-1",
            pull_request_number=42,
            pull_request_url=None,
            head_sha="abcdef1234567890",
        ),
        session,
        owner,
    )

    assert updated.pull_request_number == 42
    assert updated.pull_request_url == "https://github.com/acme/service/pull/42"
    assert updated.head_sha == "abcdef1234567890"

    monkeypatch.setattr(
        "app.main.settings",
        SimpleNamespace(github_api_enabled="", gitlab_api_enabled=""),
    )
    monkeypatch.setattr(
        "app.main.provider_secret_store",
        ProviderSecretStore(tmp_path / "provider-secrets"),
    )
    with pytest.raises(HTTPException) as exc_info:
        transition(
            requirement.id,
            TransitionRequest(
                event="begin_merge",
                expected_version=requirement.version,
                reason="",
            ),
            session,
            owner,
        )

    assert exc_info.value.status_code == 422
    assert "GITHUB_TOKEN" in str(exc_info.value.detail)
    assert session.scalars(select(MergeAttempt)).all() == []


def test_owner_schedules_automatic_pull_request_and_result_is_persisted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = Session(engine)
    owner = User(email="owner@example.com", display_name="Owner", password_hash="x")
    session.add(owner)
    session.flush()
    project = Project(key="AUTO", name="Auto PR", owner_id=owner.id)
    session.add(project)
    session.flush()
    session.add(ProjectMember(project_id=project.id, user_id=owner.id, role=ProjectRole.OWNER))
    repository = RepositoryConnection(
        project_id=project.id,
        provider="github",
        external_id="2",
        full_name="acme/service",
        clone_url="git@github.com:acme/service.git",
        web_url="https://github.com/acme/service",
    )
    session.add(repository)
    session.flush()
    requirement = Requirement(
        project_id=project.id,
        number=2,
        title="Automatic PR",
        description="Create the pull request through Git Worker",
        owner_id=owner.id,
        status=RequirementStatus.AWAITING_MERGE,
    )
    session.add(requirement)
    session.flush()
    link = RequirementRepository(
        requirement_id=requirement.id,
        repository_id=repository.id,
        target_branch="main",
        work_branch="huaban/req-2",
        head_sha="1234567890abcdef",
        status="ready",
    )
    session.add(link)
    session.commit()
    monkeypatch.setattr(
        "app.main.settings",
        SimpleNamespace(github_api_enabled="1", gitlab_api_enabled=""),
    )

    queued = create_requirement_pull_request(requirement.id, link.id, session, owner)
    task = session.get(WorkflowTask, queued["task_id"])
    assert task is not None
    assert task.task_type == "git.create_pull_request"

    process_task_result(
        session,
        {
            "task_id": task.id,
            "status": "completed",
            "output": {
                "requirement_repository_id": link.id,
                "pull_request_number": 57,
                "pull_request_url": "https://github.com/acme/service/pull/57",
                "head_sha": "1234567890abcdef",
            },
        },
    )

    assert link.pull_request_number == 57
    assert link.pull_request_url == "https://github.com/acme/service/pull/57"
    assert requirement.status == RequirementStatus.AWAITING_MERGE


def test_pull_request_task_failure_is_visible_to_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = Session(engine)
    owner = User(email="owner@example.com", display_name="Owner", password_hash="x")
    session.add(owner)
    session.flush()
    project = Project(key="FAIL", name="Failed PR", owner_id=owner.id)
    session.add(project)
    session.flush()
    session.add(ProjectMember(project_id=project.id, user_id=owner.id, role=ProjectRole.OWNER))
    repository = RepositoryConnection(
        project_id=project.id,
        provider="github",
        external_id="3",
        full_name="acme/service",
        clone_url="git@github.com:acme/service.git",
        web_url="https://github.com/acme/service",
    )
    session.add(repository)
    session.flush()
    requirement = Requirement(
        project_id=project.id,
        number=3,
        title="Failed automatic PR",
        description="Expose the provider failure to the project owner",
        owner_id=owner.id,
        status=RequirementStatus.AWAITING_MERGE,
    )
    session.add(requirement)
    session.flush()
    link = RequirementRepository(
        requirement_id=requirement.id,
        repository_id=repository.id,
        target_branch="main",
        work_branch="huaban/req-3",
        head_sha="abcdef1234567890",
        status="ready",
    )
    session.add(link)
    session.commit()
    monkeypatch.setattr(
        "app.main.settings",
        SimpleNamespace(github_api_enabled="1", gitlab_api_enabled=""),
    )

    queued = create_requirement_pull_request(requirement.id, link.id, session, owner)
    process_task_result(
        session,
        {
            "task_id": queued["task_id"],
            "status": "failed",
            "error_code": "github.http_422",
            "error_message": "Validation Failed: not all refs are readable",
            "retryable": False,
        },
    )
    session.commit()

    state = get_requirement_task(requirement.id, queued["task_id"], session, owner)
    assert state["status"] == "failed"
    assert state["error_code"] == "github.http_422"
    assert state["error_message"] == "Validation Failed: not all refs are readable"
