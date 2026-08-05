from fastapi import Cookie, Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from ..core.config import get_settings
from ..core.security import decode_access_token
from ..database import get_db
from ..models.entities import Project, ProjectMember, ProjectRole, SystemRole, User


bearer = HTTPBearer(auto_error=False)


def current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    forgeflow_session: str | None = Cookie(default=None),
    session: Session = Depends(get_db),
) -> User:
    token = credentials.credentials if credentials else forgeflow_session
    if token is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "authentication required")
    payload = decode_access_token(token, get_settings().app_secret)
    user = session.get(User, payload.get("sub")) if payload else None
    if user is None or not user.is_active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid or expired token")
    return user


def require_project_role(
    session: Session, user: User, project: Project, allowed: set[ProjectRole]
) -> None:
    if user.system_role == SystemRole.ADMIN:
        return
    membership = (
        session.query(ProjectMember)
        .filter(ProjectMember.project_id == project.id, ProjectMember.user_id == user.id)
        .one_or_none()
    )
    if membership is None or ProjectRole(membership.role) not in allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "insufficient project permission")
