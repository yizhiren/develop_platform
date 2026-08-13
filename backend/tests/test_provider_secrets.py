import stat
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.database import Base
from app.git_worker import build_provider
from app.main import (
    delete_provider_credential,
    list_provider_credentials,
    provider_capabilities,
    update_provider_credential,
)
from app.models.entities import AuditEvent, SystemRole, User
from app.providers.github import GitHubProvider
from app.schemas.domain import ProviderCredentialUpdate
from app.services.provider_secrets import ProviderSecretError, ProviderSecretStore


TOKEN = "github_pat_" + "a" * 40


def test_provider_secret_store_is_atomic_restricted_and_never_normalizes_token(tmp_path) -> None:
    store = ProviderSecretStore(tmp_path / "secrets")
    store.write("github", TOKEN)

    token_path = tmp_path / "secrets" / "github.token"
    assert store.read("github") == TOKEN
    assert stat.S_IMODE(token_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(token_path.parent.stat().st_mode) == 0o700

    with pytest.raises(ProviderSecretError, match="whitespace"):
        store.write("github", TOKEN + "\n")
    with pytest.raises(ProviderSecretError, match="unsupported"):
        store.write("unknown", TOKEN)

    store.delete("github")
    assert store.configured("github") is False


def test_admin_manages_provider_token_without_persisting_it_in_audit(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = Session(engine)
    admin = User(
        email="admin@example.com",
        display_name="Admin",
        password_hash="x",
        system_role=SystemRole.ADMIN,
    )
    member = User(email="member@example.com", display_name="Member", password_hash="x")
    session.add_all([admin, member])
    session.commit()
    store = ProviderSecretStore(tmp_path / "provider-secrets")
    monkeypatch.setattr("app.main.provider_secret_store", store)
    monkeypatch.setattr(
        "app.main.settings",
        SimpleNamespace(github_api_enabled="", gitlab_api_enabled=""),
    )

    status = update_provider_credential(
        "github",
        ProviderCredentialUpdate(token=TOKEN),
        session,
        admin,
    )

    assert status.configured is True
    assert status.source == "managed"
    assert store.read("github") == TOKEN
    assert list_provider_credentials(admin)[0].configured is True
    assert provider_capabilities(admin) == {
        "github_api_enabled": True,
        "gitlab_api_enabled": False,
    }
    audit = session.scalars(select(AuditEvent)).one()
    assert TOKEN not in audit.details_json

    with pytest.raises(HTTPException) as exc_info:
        update_provider_credential(
            "github",
            ProviderCredentialUpdate(token=TOKEN),
            session,
            member,
        )
    assert exc_info.value.status_code == 403

    removed = delete_provider_credential("github", session, admin)
    assert removed.configured is False
    assert store.read("github") == ""


@pytest.mark.asyncio
async def test_git_worker_loads_managed_token_without_environment(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = ProviderSecretStore(tmp_path / "provider-secrets")
    store.write("github", TOKEN)
    monkeypatch.setattr(
        "app.git_worker.get_settings",
        lambda: SimpleNamespace(
            provider_secret_root=store.root,
            github_token="",
            github_webhook_secret="",
            gitlab_token="",
            gitlab_webhook_secret="",
            gitlab_base_url="https://gitlab.com",
        ),
    )

    provider = build_provider("github")
    assert isinstance(provider, GitHubProvider)
    assert provider.token == TOKEN
    await provider.close()
