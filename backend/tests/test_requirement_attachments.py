import base64
from pathlib import Path

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.database import Base
from app.main import (
    create_requirement,
    list_requirement_attachments,
    requirement_attachment_content,
)
from app.models.entities import (
    Project,
    ProjectMember,
    RepositoryConnection,
    Requirement,
    RequirementAttachment,
    User,
)
from app.schemas.domain import RequirementCreate
from app.services.artifacts import ArtifactStore
from app.services.workflow import _build_task_context


PNG = b"\x89PNG\r\n\x1a\n" + b"test screenshot bytes"


def requirement_fixture() -> tuple[Session, User, Project, RepositoryConnection]:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = Session(engine)
    owner = User(email="owner@example.com", display_name="Owner", password_hash="x")
    session.add(owner)
    session.flush()
    project = Project(key="IMAGES", name="Images", owner_id=owner.id)
    session.add(project)
    session.flush()
    session.add(ProjectMember(project_id=project.id, user_id=owner.id, role="owner"))
    repository = RepositoryConnection(
        project_id=project.id,
        provider="github",
        external_id="acme-images",
        full_name="acme/images",
        clone_url="https://github.com/acme/images.git",
        web_url="https://github.com/acme/images",
        default_branch="main",
    )
    session.add(repository)
    session.commit()
    return session, owner, project, repository


def payload(repository: RepositoryConnection, *, media_type: str = "image/png") -> RequirementCreate:
    return RequirementCreate.model_validate(
        {
            "title": "Use a screenshot",
            "description": "Implement the behavior shown in the attached screenshot.",
            "repositories": [
                {
                    "repository_id": repository.id,
                    "target_branch": "main",
                    "merge_order": 0,
                }
            ],
            "attachments": [
                {
                    "filename": "../../screen.png",
                    "media_type": media_type,
                    "data_base64": base64.b64encode(PNG).decode(),
                }
            ],
        }
    )


def test_requirement_images_are_persisted_and_added_to_agent_context(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    session, owner, project, repository = requirement_fixture()
    settings = Settings(_env_file=None, artifact_root=tmp_path / "artifacts")
    monkeypatch.setattr("app.main.settings", settings)

    requirement = create_requirement(project.id, payload(repository), session, owner)

    attachment = session.scalar(select(RequirementAttachment))
    assert attachment is not None
    assert attachment.requirement_id == requirement.id
    assert attachment.filename == "screen.png"
    assert attachment.media_type == "image/png"
    assert attachment.size_bytes == len(PNG)
    assert ArtifactStore(settings.artifact_root).resolve(attachment.path).read_bytes() == PNG
    listed = list_requirement_attachments(requirement.id, session, owner)
    assert [item.id for item in listed] == [attachment.id]
    response = requirement_attachment_content(attachment.id, session, owner)
    assert Path(response.path).read_bytes() == PNG
    assert response.media_type == "image/png"
    assert response.headers["content-disposition"].startswith("inline;")

    context = _build_task_context(session, requirement)
    assert context["attachments"] == [
        {
            "id": attachment.id,
            "filename": "screen.png",
            "media_type": "image/png",
            "path": attachment.path,
            "sha256": attachment.sha256,
            "size_bytes": len(PNG),
        }
    ]
    assert "data_base64" not in context["attachments"][0]


def test_mismatched_image_content_is_rejected_before_requirement_creation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    session, owner, project, repository = requirement_fixture()
    monkeypatch.setattr(
        "app.main.settings",
        Settings(_env_file=None, artifact_root=tmp_path / "artifacts"),
    )

    with pytest.raises(HTTPException) as error:
        create_requirement(
            project.id,
            payload(repository, media_type="image/jpeg"),
            session,
            owner,
        )

    assert error.value.status_code == 422
    assert session.scalars(select(Requirement)).all() == []
