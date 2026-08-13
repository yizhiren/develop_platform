import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.main import update_repository
from app.models.entities import (
    Project,
    ProjectMember,
    ProjectRole,
    RepositoryConnection,
    Requirement,
    RequirementRepository,
    RequirementStatus,
    User,
)
from app.schemas.domain import RepositoryUpdate


def test_repository_can_be_edited_but_active_delivery_identity_is_protected() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = Session(engine)
    owner = User(email="owner@example.com", display_name="Owner", password_hash="x")
    session.add(owner)
    session.flush()
    project = Project(key="EDIT", name="Repository Edit", owner_id=owner.id)
    session.add(project)
    session.flush()
    session.add(ProjectMember(project_id=project.id, user_id=owner.id, role=ProjectRole.OWNER))
    repository = RepositoryConnection(
        project_id=project.id,
        provider="github",
        external_id="acme/service",
        full_name="acme/service",
        clone_url="git@github.com:acme/service.git",
        web_url="https://github.com/acme/service",
        default_branch="main",
    )
    session.add(repository)
    session.flush()
    requirement = Requirement(
        project_id=project.id,
        number=1,
        title="Active requirement",
        description="A requirement that still references the repository",
        owner_id=owner.id,
        status=RequirementStatus.AWAITING_MERGE,
    )
    session.add(requirement)
    session.flush()
    session.add(
        RequirementRepository(
            requirement_id=requirement.id,
            repository_id=repository.id,
            target_branch="main",
        )
    )
    session.commit()

    updated = update_repository(
        project.id,
        repository.id,
        RepositoryUpdate(
            provider="github",
            external_id="acme/service",
            full_name="acme/service",
            clone_url="git@github.com:acme/service.git",
            web_url="https://github.com/acme/service",
            default_branch="release",
        ),
        session,
        owner,
    )
    assert updated.default_branch == "release"

    with pytest.raises(HTTPException) as exc_info:
        update_repository(
            project.id,
            repository.id,
            RepositoryUpdate(
                provider="github",
                external_id="acme/renamed",
                full_name="acme/renamed",
                clone_url="git@github.com:acme/renamed.git",
                web_url="https://github.com/acme/renamed",
                default_branch="release",
            ),
            session,
            owner,
        )
    assert exc_info.value.status_code == 409
    assert repository.full_name == "acme/service"

    requirement.status = RequirementStatus.COMPLETED
    session.commit()
    renamed = update_repository(
        project.id,
        repository.id,
        RepositoryUpdate(
            provider="github",
            external_id="acme/renamed",
            full_name="acme/renamed",
            clone_url="git@github.com:acme/renamed.git",
            web_url="https://github.com/acme/renamed",
            default_branch="main",
        ),
        session,
        owner,
    )
    assert renamed.full_name == "acme/renamed"
