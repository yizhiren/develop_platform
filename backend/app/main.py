from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from urllib.parse import urlparse
from uuid import uuid4

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from redis import Redis, RedisError
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .api.deps import current_user, require_project_role
from .core.config import get_settings
from .core.security import LoginThrottle, create_access_token, hash_password, verify_password
from .database import SessionLocal, engine, get_db
from .migrations import migrate
from .models.entities import (
    ArtifactVersion,
    AgentRun,
    Approval,
    AuditEvent,
    ConversationMessage,
    Evidence,
    MergeAttempt,
    OutboxEvent,
    Project,
    ProjectMember,
    ProjectRole,
    RepositoryConnection,
    Requirement,
    RequirementAttachment,
    RequirementRepository,
    SystemRole,
    User,
    WebhookDelivery,
    WorkflowTask,
    WorkflowTransition,
)
from .providers.git import GitProviderError
from .schemas.domain import (
    LoginRequest,
    MessageCreate,
    MessageView,
    ProjectCreate,
    ProjectMemberCreate,
    ProjectMemberView,
    ProjectView,
    ProviderCredentialStatus,
    ProviderCredentialUpdate,
    RepositoryCreate,
    RepositoryUpdate,
    RepositoryView,
    RequirementCreate,
    RequirementAttachmentView,
    RequirementRepositoryView,
    RequirementRepositoryDeliveryUpdate,
    RequirementView,
    TokenResponse,
    TransitionRequest,
    TransitionView,
    UserView,
    UserCreate,
)
from .services.outbox import OutboxScheduler
from .services.leases import requirement_cancellation_key
from .services.artifacts import ArtifactStore, ArtifactStoreError
from .services.requirement_attachments import (
    RequirementAttachmentError,
    decode_requirement_images,
)
from .services.repository_urls import expected_provider_host, pull_request_web_url, validate_clone_url, validate_pull_request_url
from .services.provider_secrets import ProviderSecretError, ProviderSecretStore
from .services.workflow import VersionConflict, WorkflowError, transition_requirement


settings = get_settings()
logger = logging.getLogger(__name__)
login_throttle = LoginThrottle()
provider_secret_store = ProviderSecretStore(settings.provider_secret_root)


def _utc_iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    aware = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    return aware.isoformat().replace("+00:00", "Z")


def provider_api_enabled(provider: str) -> bool:
    if provider == "github":
        return provider_secret_store.configured(provider) or bool(settings.github_api_enabled)
    if provider == "gitlab":
        return provider_secret_store.configured(provider) or bool(settings.gitlab_api_enabled)
    return False


def mark_requirement_cancelled(requirement_id: str) -> None:
    """Best-effort data-plane signal; SQLite remains the workflow source of truth."""
    client = Redis.from_url(
        settings.redis_url,
        decode_responses=True,
        socket_connect_timeout=2,
        socket_timeout=2,
    )
    try:
        client.set(requirement_cancellation_key(requirement_id), "1")
    except RedisError:
        logger.warning(
            "failed to publish requirement cancellation marker: requirement_id=%s",
            requirement_id,
            exc_info=True,
        )
    finally:
        client.close()


def require_system_admin(user: User) -> None:
    if user.system_role != SystemRole.ADMIN:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "system administrator role required")


def provider_credential_status(provider: str) -> ProviderCredentialStatus:
    if provider not in {"github", "gitlab"}:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "provider not found")
    managed = provider_secret_store.configured(provider)
    environment = bool(
        settings.github_api_enabled if provider == "github" else settings.gitlab_api_enabled
    )
    source = "managed" if managed else "environment" if environment else "none"
    return ProviderCredentialStatus(
        provider=provider,
        configured=managed or environment,
        source=source,
    )


def bootstrap() -> None:
    if settings.app_env == "production" and settings.app_secret == "development-only-change-me-32-chars":
        raise RuntimeError("APP_SECRET must be changed in production")
    if settings.app_env == "production" and settings.bootstrap_admin_password == "change-this-password":
        raise RuntimeError("BOOTSTRAP_ADMIN_PASSWORD must be changed in production")
    migrate(engine)
    with SessionLocal() as session:
        user = session.scalar(select(User).where(User.email == settings.bootstrap_admin_email.lower()))
        if user is None:
            session.add(
                User(
                    email=settings.bootstrap_admin_email.lower(),
                    display_name="平台管理员",
                    password_hash=hash_password(settings.bootstrap_admin_password),
                    system_role=SystemRole.ADMIN,
                )
            )
            session.commit()


@asynccontextmanager
async def lifespan(app: FastAPI):
    bootstrap()
    scheduler = OutboxScheduler()
    task = asyncio.create_task(scheduler.run())
    app.state.scheduler = scheduler
    yield
    await scheduler.close()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


app = FastAPI(title="画板 Control Plane", version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.web_origin],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def request_context(request: Request, call_next):
    request.state.request_id = request.headers.get("x-request-id") or str(uuid4())
    response = await call_next(request)
    response.headers["X-Request-ID"] = request.state.request_id
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    return response


@app.exception_handler(HTTPException)
async def http_error(request: Request, exc: HTTPException) -> JSONResponse:
    code = {
        401: "auth.unauthorized", 403: "auth.forbidden", 404: "resource.not_found",
        409: "resource.conflict", 422: "request.unprocessable",
    }.get(exc.status_code, "request.failed")
    return JSONResponse(status_code=exc.status_code, content={"error": {"code": code, "message": str(exc.detail), "details": {}, "request_id": request.state.request_id}})


@app.exception_handler(RequestValidationError)
async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
    errors = [{"loc": list(item["loc"]), "msg": item["msg"], "type": item["type"]} for item in exc.errors()]
    return JSONResponse(status_code=422, content={"error": {"code": "request.validation", "message": "request validation failed", "details": {"errors": errors}, "request_id": request.state.request_id}})


@app.get("/health/live")
def live() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/health/ready")
def ready(session: Session = Depends(get_db)) -> dict[str, str]:
    session.execute(select(1))
    return {"status": "ready", "database": "sqlite", "workflow": "outbox"}


@app.post("/api/v1/auth/login", response_model=TokenResponse)
def login(payload: LoginRequest, request: Request, response: Response, session: Session = Depends(get_db)) -> TokenResponse:
    client = request.client.host if request.client else "unknown"
    throttle_key = f"{client}:{payload.email.lower()}"
    if not login_throttle.is_allowed(throttle_key):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "too many failed login attempts")
    user = session.scalar(select(User).where(User.email == payload.email.lower()))
    if user is None or not verify_password(payload.password, user.password_hash):
        login_throttle.record_failure(throttle_key)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid credentials")
    login_throttle.reset(throttle_key)
    token = create_access_token(user.id, settings.app_secret)
    response.set_cookie(
        "forgeflow_session",
        token,
        httponly=True,
        secure=settings.app_env == "production",
        samesite="lax",
        max_age=8 * 3600,
    )
    return TokenResponse(access_token=token)


@app.post("/api/v1/auth/logout", status_code=204, response_class=Response)
def logout(response: Response) -> Response:
    response.delete_cookie("forgeflow_session")
    response.status_code = status.HTTP_204_NO_CONTENT
    return response


@app.get("/api/v1/auth/me", response_model=UserView)
def me(user: User = Depends(current_user)) -> User:
    return user


@app.get("/api/v1/provider-capabilities")
def provider_capabilities(user: User = Depends(current_user)) -> dict[str, bool]:
    del user
    return {
        "github_api_enabled": provider_api_enabled("github"),
        "gitlab_api_enabled": provider_api_enabled("gitlab"),
    }


@app.get("/api/v1/admin/provider-credentials", response_model=list[ProviderCredentialStatus])
def list_provider_credentials(user: User = Depends(current_user)) -> list[ProviderCredentialStatus]:
    require_system_admin(user)
    return [provider_credential_status("github"), provider_credential_status("gitlab")]


@app.put("/api/v1/admin/provider-credentials/{provider}", response_model=ProviderCredentialStatus)
def update_provider_credential(
    provider: str,
    payload: ProviderCredentialUpdate,
    session: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> ProviderCredentialStatus:
    require_system_admin(user)
    if provider not in {"github", "gitlab"}:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "provider not found")
    try:
        provider_secret_store.write(provider, payload.token.get_secret_value())
    except ProviderSecretError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    session.add(
        AuditEvent(
            actor_id=user.id,
            action="provider.credential_update",
            resource_type="provider",
            resource_id=provider,
            details_json=json.dumps({"configured": True}),
        )
    )
    session.commit()
    return provider_credential_status(provider)


@app.delete("/api/v1/admin/provider-credentials/{provider}", response_model=ProviderCredentialStatus)
def delete_provider_credential(
    provider: str,
    session: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> ProviderCredentialStatus:
    require_system_admin(user)
    if provider not in {"github", "gitlab"}:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "provider not found")
    provider_secret_store.delete(provider)
    session.add(
        AuditEvent(
            actor_id=user.id,
            action="provider.credential_delete",
            resource_type="provider",
            resource_id=provider,
            details_json=json.dumps({"configured": False}),
        )
    )
    session.commit()
    return provider_credential_status(provider)


def require_admin(user: User) -> None:
    if user.system_role != SystemRole.ADMIN:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "administrator permission required")


@app.get("/api/v1/users", response_model=list[UserView])
def list_users(
    session: Session = Depends(get_db), user: User = Depends(current_user)
) -> list[User]:
    require_admin(user)
    return list(session.scalars(select(User).order_by(User.created_at)).all())


@app.post("/api/v1/users", response_model=UserView, status_code=201)
def create_user(
    payload: UserCreate,
    session: Session = Depends(get_db),
    actor: User = Depends(current_user),
) -> User:
    require_admin(actor)
    created = User(
        email=payload.email.lower(),
        display_name=payload.display_name,
        password_hash=hash_password(payload.password),
        system_role=payload.system_role,
    )
    session.add(created)
    try:
        session.flush()
    except IntegrityError as exc:
        session.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "user email already exists") from exc
    session.add(AuditEvent(actor_id=actor.id, action="user.create", resource_type="user", resource_id=created.id))
    session.commit()
    session.refresh(created)
    return created


@app.get("/api/v1/projects", response_model=list[ProjectView])
def list_projects(
    session: Session = Depends(get_db), user: User = Depends(current_user)
) -> list[Project]:
    query = select(Project).order_by(Project.created_at.desc())
    if user.system_role != SystemRole.ADMIN:
        query = query.join(ProjectMember).where(ProjectMember.user_id == user.id)
    return list(session.scalars(query).all())


@app.post("/api/v1/projects", response_model=ProjectView, status_code=201)
def create_project(
    payload: ProjectCreate,
    session: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> Project:
    if session.scalar(select(Project).where(Project.key == payload.key)):
        raise HTTPException(status.HTTP_409_CONFLICT, "project key already exists")
    project = Project(**payload.model_dump(), owner_id=user.id)
    session.add(project)
    session.flush()
    session.add(ProjectMember(project_id=project.id, user_id=user.id, role=ProjectRole.OWNER))
    session.add(AuditEvent(actor_id=user.id, action="project.create", resource_type="project", resource_id=project.id))
    session.commit()
    session.refresh(project)
    return project


def load_project(session: Session, project_id: str) -> Project:
    project = session.get(Project, project_id)
    if project is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "project not found")
    return project


@app.get("/api/v1/projects/{project_id}/members", response_model=list[ProjectMemberView])
def list_project_members(
    project_id: str,
    session: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> list[ProjectMemberView]:
    project = load_project(session, project_id)
    require_project_role(session, user, project, set(ProjectRole))
    rows = session.execute(
        select(ProjectMember, User)
        .join(User, User.id == ProjectMember.user_id)
        .where(ProjectMember.project_id == project_id)
        .order_by(User.display_name)
    ).all()
    return [ProjectMemberView(user_id=member.user_id, email=member_user.email, display_name=member_user.display_name, role=member.role) for member, member_user in rows]


@app.put("/api/v1/projects/{project_id}/members", response_model=ProjectMemberView)
def upsert_project_member(
    project_id: str,
    payload: ProjectMemberCreate,
    session: Session = Depends(get_db),
    actor: User = Depends(current_user),
) -> ProjectMemberView:
    project = load_project(session, project_id)
    require_project_role(session, actor, project, {ProjectRole.OWNER})
    member_user = session.get(User, payload.user_id)
    if member_user is None or not member_user.is_active:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "user not found")
    membership = session.scalar(select(ProjectMember).where(ProjectMember.project_id == project_id, ProjectMember.user_id == payload.user_id))
    if membership is None:
        membership = ProjectMember(project_id=project_id, user_id=payload.user_id, role=payload.role)
        session.add(membership)
    else:
        membership.role = payload.role
    session.add(AuditEvent(actor_id=actor.id, action="project.member_upsert", resource_type="project", resource_id=project_id, details_json=json.dumps({"user_id": payload.user_id, "role": payload.role})))
    session.commit()
    return ProjectMemberView(user_id=member_user.id, email=member_user.email, display_name=member_user.display_name, role=membership.role)


@app.get("/api/v1/projects/{project_id}/repositories", response_model=list[RepositoryView])
def list_repositories(
    project_id: str,
    session: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> list[RepositoryConnection]:
    project = load_project(session, project_id)
    require_project_role(session, user, project, set(ProjectRole))
    return list(session.scalars(select(RepositoryConnection).where(RepositoryConnection.project_id == project_id)).all())


@app.post("/api/v1/projects/{project_id}/repositories", response_model=RepositoryView, status_code=201)
def create_repository(
    project_id: str,
    payload: RepositoryCreate,
    session: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> RepositoryConnection:
    project = load_project(session, project_id)
    require_project_role(session, user, project, {ProjectRole.OWNER})
    _validate_repository_urls(payload)
    repo = RepositoryConnection(project_id=project.id, **payload.model_dump())
    session.add(repo)
    try:
        session.flush()
    except IntegrityError as exc:
        session.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "repository is already connected") from exc
    session.add(AuditEvent(actor_id=user.id, action="repository.connect", resource_type="repository", resource_id=repo.id))
    session.commit()
    session.refresh(repo)
    return repo


@app.put(
    "/api/v1/projects/{project_id}/repositories/{repository_id}",
    response_model=RepositoryView,
)
def update_repository(
    project_id: str,
    repository_id: str,
    payload: RepositoryUpdate,
    session: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> RepositoryConnection:
    project = load_project(session, project_id)
    require_project_role(session, user, project, {ProjectRole.OWNER})
    repository = session.get(RepositoryConnection, repository_id)
    if repository is None or repository.project_id != project_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "repository not found")
    _validate_repository_urls(payload)

    identity_fields = {"provider", "external_id", "full_name", "clone_url"}
    changed_fields = {
        field
        for field, value in payload.model_dump().items()
        if getattr(repository, field) != value
    }
    active_usage = session.scalar(
        select(func.count())
        .select_from(RequirementRepository)
        .join(Requirement, Requirement.id == RequirementRepository.requirement_id)
        .where(
            RequirementRepository.repository_id == repository.id,
            Requirement.status.notin_({"completed", "cancelled"}),
        )
    ) or 0
    if active_usage and changed_fields.intersection(identity_fields):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "repository identity or clone URL cannot change while active requirements reference it",
        )

    for field, value in payload.model_dump().items():
        setattr(repository, field, value)
    try:
        session.flush()
    except IntegrityError as exc:
        session.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "repository identity is already connected") from exc
    session.add(
        AuditEvent(
            actor_id=user.id,
            action="repository.update",
            resource_type="repository",
            resource_id=repository.id,
            details_json=json.dumps({"changed_fields": sorted(changed_fields)}),
        )
    )
    session.commit()
    session.refresh(repository)
    return repository


def _validate_repository_urls(payload: RepositoryCreate | RepositoryUpdate) -> None:
    web = urlparse(payload.web_url)
    expected_host = expected_provider_host(payload.provider, settings.gitlab_base_url)
    try:
        validate_clone_url(payload.provider, payload.clone_url, settings.gitlab_base_url)
    except GitProviderError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    if (
        web.scheme != "https"
        or web.username
        or web.password
        or (web.hostname or "").lower() != expected_host
    ):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "web URL must be credential-free HTTPS on the configured provider host",
        )
    if payload.full_name.count("/") < 1 or payload.full_name.startswith("/") or payload.full_name.endswith("/"):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "repository full_name must include owner and repository")


@app.delete("/api/v1/projects/{project_id}/repositories/{repository_id}", status_code=204, response_class=Response)
def disconnect_repository(
    project_id: str,
    repository_id: str,
    session: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> Response:
    project = load_project(session, project_id)
    require_project_role(session, user, project, {ProjectRole.OWNER})
    repository = session.get(RepositoryConnection, repository_id)
    if repository is None or repository.project_id != project_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "repository not found")
    usage = session.scalar(
        select(func.count()).select_from(RequirementRepository).where(
            RequirementRepository.repository_id == repository_id
        )
    ) or 0
    if usage:
        raise HTTPException(status.HTTP_409_CONFLICT, "repository is referenced by requirements")
    session.add(AuditEvent(actor_id=user.id, action="repository.disconnect", resource_type="repository", resource_id=repository.id))
    session.delete(repository)
    session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@app.get("/api/v1/projects/{project_id}/requirements", response_model=list[RequirementView])
def list_requirements(
    project_id: str,
    session: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> list[Requirement]:
    project = load_project(session, project_id)
    require_project_role(session, user, project, set(ProjectRole))
    return list(session.scalars(select(Requirement).where(Requirement.project_id == project_id).order_by(Requirement.number.desc())).all())


@app.post("/api/v1/projects/{project_id}/requirements", response_model=RequirementView, status_code=201)
def create_requirement(
    project_id: str,
    payload: RequirementCreate,
    session: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> Requirement:
    project = load_project(session, project_id)
    require_project_role(session, user, project, {ProjectRole.OWNER, ProjectRole.DEVELOPER})
    repository_ids = {item.repository_id for item in payload.repositories}
    found = set(session.scalars(select(RepositoryConnection.id).where(RepositoryConnection.project_id == project_id, RepositoryConnection.id.in_(repository_ids))).all())
    if found != repository_ids:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "one or more repositories do not belong to this project")
    try:
        decoded_attachments = decode_requirement_images(payload.attachments)
    except RequirementAttachmentError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    number = int(session.scalar(select(func.max(Requirement.number)).where(Requirement.project_id == project_id)) or 0) + 1
    requirement = Requirement(
        project_id=project_id,
        number=number,
        title=payload.title,
        description=payload.description,
        priority=payload.priority,
        owner_id=user.id,
    )
    session.add(requirement)
    session.flush()
    store = ArtifactStore(settings.artifact_root)
    stored_paths: list[str] = []
    try:
        for item in payload.repositories:
            session.add(RequirementRepository(requirement_id=requirement.id, **item.model_dump()))
        for index, attachment in enumerate(decoded_attachments, start=1):
            path, digest, size = store.write(
                requirement.id,
                f"input-image-{index}",
                attachment.content,
            )
            stored_paths.append(path)
            session.add(
                RequirementAttachment(
                    requirement_id=requirement.id,
                    filename=attachment.filename,
                    media_type=attachment.media_type,
                    path=path,
                    sha256=digest,
                    size_bytes=size,
                )
            )
        session.add(
            AuditEvent(
                actor_id=user.id,
                action="requirement.create",
                resource_type="requirement",
                resource_id=requirement.id,
                details_json=json.dumps({"attachment_count": len(decoded_attachments)}),
            )
        )
        session.commit()
    except Exception:
        session.rollback()
        for path in stored_paths:
            try:
                store.resolve(path).unlink(missing_ok=True)
            except (ArtifactStoreError, OSError):
                pass
        raise
    session.refresh(requirement)
    return requirement


@app.get("/api/v1/requirements/{requirement_id}", response_model=RequirementView)
def get_requirement(
    requirement_id: str,
    session: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> Requirement:
    requirement = session.get(Requirement, requirement_id)
    if requirement is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "requirement not found")
    project = load_project(session, requirement.project_id)
    require_project_role(session, user, project, set(ProjectRole))
    return requirement


@app.get(
    "/api/v1/requirements/{requirement_id}/attachments",
    response_model=list[RequirementAttachmentView],
)
def list_requirement_attachments(
    requirement_id: str,
    session: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> list[RequirementAttachment]:
    get_requirement(requirement_id, session, user)
    return list(
        session.scalars(
            select(RequirementAttachment)
            .where(RequirementAttachment.requirement_id == requirement_id)
            .order_by(RequirementAttachment.created_at, RequirementAttachment.id)
        ).all()
    )


@app.get("/api/v1/requirement-attachments/{attachment_id}/content")
def requirement_attachment_content(
    attachment_id: str,
    session: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> FileResponse:
    attachment = session.get(RequirementAttachment, attachment_id)
    if attachment is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "requirement attachment not found")
    get_requirement(attachment.requirement_id, session, user)
    try:
        path = ArtifactStore(settings.artifact_root).resolve(attachment.path)
    except ArtifactStoreError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "requirement attachment file not found") from exc
    return FileResponse(
        path,
        media_type=attachment.media_type,
        filename=attachment.filename,
        content_disposition_type="inline",
        headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "private, max-age=3600"},
    )


@app.get("/api/v1/requirements/{requirement_id}/repositories", response_model=list[RequirementRepositoryView])
def list_requirement_repositories(
    requirement_id: str,
    session: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> list[RequirementRepository]:
    get_requirement(requirement_id, session, user)
    return list(session.scalars(select(RequirementRepository).where(RequirementRepository.requirement_id == requirement_id).order_by(RequirementRepository.merge_order)).all())


@app.patch("/api/v1/requirements/{requirement_id}/repositories/{link_id}", response_model=RequirementRepositoryView)
def update_requirement_repository_delivery(
    requirement_id: str,
    link_id: str,
    payload: RequirementRepositoryDeliveryUpdate,
    session: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> RequirementRepository:
    requirement = get_requirement(requirement_id, session, user)
    project = load_project(session, requirement.project_id)
    require_project_role(session, user, project, {ProjectRole.OWNER})
    link = session.get(RequirementRepository, link_id)
    if link is None or link.requirement_id != requirement_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "requirement repository not found")
    repository = session.get(RepositoryConnection, link.repository_id)
    if repository is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "repository not found")
    if requirement.status != "awaiting_merge" or link.status != "ready":
        raise HTTPException(status.HTTP_409_CONFLICT, "pull request can only be registered at the merge gate")
    if payload.work_branch != link.work_branch or payload.head_sha.lower() != (link.head_sha or "").lower():
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "reviewed branch or head SHA does not match")
    pull_request_url = payload.pull_request_url or pull_request_web_url(
        repository.provider,
        repository.full_name,
        payload.pull_request_number,
        settings.gitlab_base_url,
    )
    try:
        validate_pull_request_url(
            repository.provider,
            repository.full_name,
            payload.pull_request_number,
            pull_request_url,
            settings.gitlab_base_url,
        )
    except GitProviderError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    link.pull_request_number = payload.pull_request_number
    link.pull_request_url = pull_request_url
    session.add(AuditEvent(actor_id=user.id, action="requirement.repository_delivery_update", resource_type="requirement", resource_id=requirement_id, details_json=json.dumps({"link_id": link_id, "pull_request_number": payload.pull_request_number})))
    session.commit()
    session.refresh(link)
    return link


@app.post("/api/v1/requirements/{requirement_id}/repositories/{link_id}/pull-request", status_code=202)
def create_requirement_pull_request(
    requirement_id: str,
    link_id: str,
    session: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict[str, str]:
    requirement = get_requirement(requirement_id, session, user)
    project = load_project(session, requirement.project_id)
    require_project_role(session, user, project, {ProjectRole.OWNER})
    link = session.get(RequirementRepository, link_id)
    if link is None or link.requirement_id != requirement_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "requirement repository not found")
    repository = session.get(RepositoryConnection, link.repository_id)
    if repository is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "repository not found")
    if requirement.status != "awaiting_merge" or link.status != "ready":
        raise HTTPException(status.HTTP_409_CONFLICT, "pull request can only be created at the merge gate")
    if link.pull_request_number is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "pull request is already registered")
    if not link.work_branch or not link.head_sha:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "reviewed branch and head SHA are required")
    if not provider_api_enabled(repository.provider):
        variable = "GITHUB_TOKEN" if repository.provider == "github" else "GITLAB_TOKEN"
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"未配置 Provider API Token；请在 .env.local 配置 {variable} 并重启 control-plane 与 git-worker",
        )

    idempotency_key = f"{requirement.id}:{link.id}:{link.head_sha}:git.create_pull_request"
    existing = session.scalar(select(WorkflowTask).where(WorkflowTask.idempotency_key == idempotency_key))
    if existing is not None and existing.status in {"queued", "running"}:
        return {"task_id": existing.id, "status": existing.status}
    if existing is not None:
        idempotency_key = f"{idempotency_key}:retry:{uuid4()}"

    context = {
        "requirement_id": requirement.id,
        "requirement_repository_id": link.id,
        "provider": repository.provider,
        "repository": repository.full_name,
        "work_branch": link.work_branch,
        "target_branch": link.target_branch,
        "head_sha": link.head_sha,
        "title": requirement.title,
        "description": requirement.description,
    }
    payload = {
        "requirement_id": requirement.id,
        "requirement_version": requirement.version,
        "agent_run_id": None,
        "context": context,
    }
    task = WorkflowTask(
        requirement_id=requirement.id,
        task_type="git.create_pull_request",
        idempotency_key=idempotency_key,
        payload_json=json.dumps(payload, ensure_ascii=False),
    )
    session.add(task)
    session.flush()
    session.add(
        OutboxEvent(
            topic="forgeflow.tasks",
            aggregate_id=requirement.id,
            payload_json=json.dumps(
                {"task_id": task.id, "task_type": task.task_type, "payload": payload},
                ensure_ascii=False,
            ),
        )
    )
    session.add(
        AuditEvent(
            actor_id=user.id,
            action="requirement.pull_request_create_requested",
            resource_type="requirement",
            resource_id=requirement.id,
            details_json=json.dumps({"link_id": link.id, "head_sha": link.head_sha}),
        )
    )
    session.commit()
    return {"task_id": task.id, "status": task.status}


@app.get("/api/v1/requirements/{requirement_id}/tasks/{task_id}")
def get_requirement_task(
    requirement_id: str,
    task_id: str,
    session: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict[str, Any]:
    get_requirement(requirement_id, session, user)
    task = session.get(WorkflowTask, task_id)
    if task is None or task.requirement_id != requirement_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "workflow task not found")
    effective_task = task
    if task.status == "failed":
        retry = session.scalar(
            select(WorkflowTask)
            .where(
                WorkflowTask.requirement_id == requirement_id,
                WorkflowTask.idempotency_key.like(f"{task.idempotency_key}:retry:%"),
            )
            .order_by(WorkflowTask.created_at.desc())
        )
        if retry is not None:
            effective_task = retry
    payload = json.loads(effective_task.payload_json)
    failure = payload.get("_failure") if isinstance(payload.get("_failure"), dict) else {}
    return {
        "task_id": effective_task.id,
        "task_type": effective_task.task_type,
        "status": effective_task.status,
        "error_code": failure.get("error_code"),
        "error_message": failure.get("error_message"),
        "diagnostics": failure.get("diagnostics") or {},
    }


@app.get("/api/v1/requirements/{requirement_id}/tasks")
def list_requirement_tasks(
    requirement_id: str,
    session: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> list[dict]:
    get_requirement(requirement_id, session, user)
    tasks = session.scalars(
        select(WorkflowTask)
        .where(WorkflowTask.requirement_id == requirement_id)
        .order_by(WorkflowTask.created_at, WorkflowTask.id)
    ).all()
    return [
        {
            "id": task.id,
            "agent_run_id": task.agent_run_id,
            "task_type": task.task_type,
            "status": task.status,
            "created_at": _utc_iso(task.created_at),
        }
        for task in tasks
    ]


@app.get("/api/v1/requirements/{requirement_id}/messages", response_model=list[MessageView])
def list_messages(
    requirement_id: str,
    session: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> list[ConversationMessage]:
    get_requirement(requirement_id, session, user)
    return list(session.scalars(select(ConversationMessage).where(ConversationMessage.requirement_id == requirement_id).order_by(ConversationMessage.created_at)).all())


@app.post("/api/v1/requirements/{requirement_id}/messages", response_model=MessageView, status_code=201)
def create_message(
    requirement_id: str,
    payload: MessageCreate,
    session: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> ConversationMessage:
    requirement = get_requirement(requirement_id, session, user)
    message = ConversationMessage(requirement_id=requirement_id, author_type="user", author_id=user.id, stage=requirement.status, body=payload.body)
    session.add(message)
    session.flush()
    session.add(AuditEvent(actor_id=user.id, action="requirement.comment", resource_type="requirement", resource_id=requirement_id))
    session.commit()
    session.refresh(message)
    return message


@app.post("/api/v1/requirements/{requirement_id}/transitions", response_model=TransitionView)
def transition(
    requirement_id: str,
    payload: TransitionRequest,
    session: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> TransitionView:
    requirement = session.get(Requirement, requirement_id)
    if requirement is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "requirement not found")
    project = load_project(session, requirement.project_id)
    require_project_role(session, user, project, {ProjectRole.OWNER, ProjectRole.DEVELOPER})
    human_events = {
        "publish", "cancel", "pause", "resume", "confirm_clarification",
        "request_more_clarification", "confirm_plan", "request_plan_change",
        "begin_merge", "retry_clarification", "retry_development", "retry_review", "retry_planning", "retry_acceptance", "retry_merge", "retry_regression",
    }
    if payload.event not in human_events:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "event is reserved for an internal worker")
    if payload.event == "cancel" and user.id != requirement.owner_id:
        require_project_role(session, user, project, {ProjectRole.OWNER})
    owner_only = {"confirm_clarification", "confirm_plan", "begin_merge", "retry_clarification", "retry_development", "retry_review", "retry_planning", "retry_acceptance", "retry_merge", "retry_regression"}
    if payload.event in owner_only:
        require_project_role(session, user, project, {ProjectRole.OWNER})
    task_context = None
    if payload.event == "begin_merge":
        target = session.scalar(select(RequirementRepository).where(RequirementRepository.requirement_id == requirement.id, RequirementRepository.status != "merged").order_by(RequirementRepository.merge_order))
        if target is None:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "no repository remains to merge")
        repository = session.get(RepositoryConnection, target.repository_id)
        if repository is None or target.pull_request_number is None or not target.head_sha:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "next repository has no ready pull request and head SHA")
        if not provider_api_enabled(repository.provider):
            variable = "GITHUB_TOKEN" if repository.provider == "github" else "GITLAB_TOKEN"
            provider_name = "GitHub" if repository.provider == "github" else "GitLab"
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                f"未配置 {provider_name} API Token，无法执行自动合并；请配置 {variable} 后重启服务",
            )
        attempt = MergeAttempt(requirement_id=requirement.id, requirement_repository_id=target.id, expected_head_sha=target.head_sha, actor_id=user.id)
        session.add(attempt)
        session.flush()
        task_context = {"merge_attempt_id": attempt.id, "requirement_repository_id": target.id, "provider": repository.provider, "repository": repository.full_name, "pull_request_number": target.pull_request_number, "head_sha": target.head_sha}
    try:
        task = transition_requirement(session, requirement, payload.event, payload.expected_version, "user", user.id, payload.reason, task_context=task_context)
    except VersionConflict as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except WorkflowError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    if payload.event in {"confirm_clarification", "confirm_plan", "begin_merge", "request_more_clarification", "request_plan_change"}:
        artifact_kind = {
            "confirm_clarification": "clarification_spec",
            "request_more_clarification": "clarification_spec",
            "confirm_plan": "architecture_plan",
            "request_plan_change": "architecture_plan",
        }.get(payload.event)
        artifact = session.scalar(select(ArtifactVersion).where(ArtifactVersion.requirement_id == requirement.id, ArtifactVersion.kind == artifact_kind).order_by(ArtifactVersion.version.desc())) if artifact_kind else None
        session.add(Approval(requirement_id=requirement.id, artifact_id=artifact.id if artifact else None, kind=payload.event, decision="approved" if payload.event.startswith(("confirm", "begin")) else "changes_requested", actor_id=user.id, comment=payload.reason))
    session.add(AuditEvent(actor_id=user.id, action=f"requirement.{payload.event}", resource_type="requirement", resource_id=requirement.id, details_json=json.dumps({"reason": payload.reason})))
    session.commit()
    if payload.event == "cancel":
        mark_requirement_cancelled(requirement.id)
    session.refresh(requirement)
    return TransitionView(requirement=RequirementView.model_validate(requirement), scheduled_task_id=task.id if task else None)


@app.get("/api/v1/requirements/{requirement_id}/artifacts")
def list_artifacts(
    requirement_id: str,
    session: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> list[dict]:
    requirement = get_requirement(requirement_id, session, user)
    del requirement
    artifacts = session.scalars(select(ArtifactVersion).where(ArtifactVersion.requirement_id == requirement_id).order_by(ArtifactVersion.created_at)).all()
    return [{"id": item.id, "kind": item.kind, "version": item.version, "schema_version": item.schema_version, "content": json.loads(item.content_json), "markdown": item.content_markdown, "created_at": _utc_iso(item.created_at)} for item in artifacts]


@app.get("/api/v1/requirements/{requirement_id}/evidence")
def list_evidence(
    requirement_id: str,
    session: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> list[dict]:
    get_requirement(requirement_id, session, user)
    items = session.scalars(
        select(Evidence).where(Evidence.requirement_id == requirement_id).order_by(Evidence.created_at)
    ).all()
    return [
        {
            "id": item.id,
            "kind": item.kind,
            "sha256": item.sha256,
            "size_bytes": item.size_bytes,
            "created_at": _utc_iso(item.created_at),
        }
        for item in items
    ]


@app.get("/api/v1/evidence/{evidence_id}/download")
def download_evidence(
    evidence_id: str,
    session: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> FileResponse:
    evidence = session.get(Evidence, evidence_id)
    if evidence is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "evidence not found")
    get_requirement(evidence.requirement_id, session, user)
    try:
        path = ArtifactStore(settings.artifact_root).resolve(evidence.path)
    except ArtifactStoreError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "evidence file not found") from exc
    return FileResponse(
        path,
        media_type="application/octet-stream",
        filename=f"{evidence.kind}-{evidence.sha256[:12]}.bin",
    )


@app.get("/api/v1/requirements/{requirement_id}/timeline")
def timeline(
    requirement_id: str,
    session: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> list[dict]:
    get_requirement(requirement_id, session, user)
    transitions = session.scalars(select(WorkflowTransition).where(WorkflowTransition.requirement_id == requirement_id).order_by(WorkflowTransition.created_at)).all()
    return [{"id": item.id, "from_status": item.from_status, "to_status": item.to_status, "event": item.event, "actor_type": item.actor_type, "actor_id": item.actor_id, "reason": item.reason, "created_at": _utc_iso(item.created_at)} for item in transitions]


@app.get("/api/v1/requirements/{requirement_id}/agent-runs")
def list_agent_runs(
    requirement_id: str,
    session: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> list[dict]:
    get_requirement(requirement_id, session, user)
    runs = session.scalars(
        select(AgentRun).where(AgentRun.requirement_id == requirement_id).order_by(AgentRun.created_at)
    ).all()
    return [
        {
            "id": run.id,
            "agent_key": run.agent_key,
            "role": run.role,
            "status": run.status,
            "model": run.model,
            "prompt_version": run.prompt_version,
            "token_usage": run.token_usage,
            "error_code": run.error_code,
            "error_message": run.error_message,
            "diagnostics": json.loads(run.diagnostics_json or "{}"),
            "created_at": _utc_iso(run.created_at),
            "completed_at": _utc_iso(run.completed_at),
        }
        for run in runs
    ]


@app.get("/api/v1/audit")
def list_audit_events(
    limit: int = 100,
    session: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> list[dict]:
    require_admin(user)
    safe_limit = min(max(limit, 1), 500)
    items = session.scalars(select(AuditEvent).order_by(AuditEvent.created_at.desc()).limit(safe_limit)).all()
    return [{"id": item.id, "actor_id": item.actor_id, "action": item.action, "resource_type": item.resource_type, "resource_id": item.resource_id, "details": json.loads(item.details_json), "created_at": _utc_iso(item.created_at)} for item in items]


@app.get("/api/v1/events")
async def events(user: User = Depends(current_user)) -> StreamingResponse:
    async def stream():
        sequence = 0
        while True:
            sequence += 1
            payload = {"event_id": f"heartbeat-{sequence}", "event_type": "heartbeat", "project_id": None, "requirement_id": None, "agent_run_id": None, "sequence": sequence, "payload": {"user_id": user.id}}
            yield f"event: heartbeat\ndata: {json.dumps(payload)}\n\n"
            await asyncio.sleep(15)
    return StreamingResponse(stream(), media_type="text/event-stream")


@app.post("/api/v1/webhooks/github", status_code=202)
async def github_webhook(request: Request, x_hub_signature_256: str | None = Header(default=None), x_github_delivery: str | None = Header(default=None), x_github_event: str | None = Header(default=None), session: Session = Depends(get_db)) -> dict[str, bool]:
    body = await request.body()
    secret = settings.github_webhook_secret
    if secret:
        expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        if not x_hub_signature_256 or not hmac.compare_digest(expected, x_hub_signature_256):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid webhook signature")
    return record_webhook(session, "github", x_github_delivery, x_github_event, body)


@app.post("/api/v1/webhooks/gitlab", status_code=202)
async def gitlab_webhook(request: Request, x_gitlab_token: str | None = Header(default=None), x_gitlab_event_uuid: str | None = Header(default=None), x_gitlab_event: str | None = Header(default=None), session: Session = Depends(get_db)) -> dict[str, bool]:
    body = await request.body()
    secret = settings.gitlab_webhook_secret
    if secret and not hmac.compare_digest(secret, x_gitlab_token or ""):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid webhook token")
    return record_webhook(session, "gitlab", x_gitlab_event_uuid, x_gitlab_event, body)


def record_webhook(session: Session, provider: str, delivery_id: str | None, event_type: str | None, body: bytes) -> dict[str, bool]:
    if not delivery_id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "webhook delivery id is required")
    if session.scalar(select(WebhookDelivery).where(WebhookDelivery.provider == provider, WebhookDelivery.delivery_id == delivery_id)):
        return {"accepted": True, "duplicate": True}
    session.add(WebhookDelivery(provider=provider, delivery_id=delivery_id, event_type=event_type or "unknown", payload_sha256=hashlib.sha256(body).hexdigest()))
    session.commit()
    return {"accepted": True, "duplicate": False}
