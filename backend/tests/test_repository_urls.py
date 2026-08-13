from pathlib import Path

import pytest

from app.core.config import Settings
from app.providers.git import GitProviderError
from app.services.git_workspace import GitWorkspaceManager
from app.services.repository_urls import validate_clone_url, validate_pull_request_url


@pytest.mark.parametrize(
    ("provider", "clone_url", "gitlab_base_url"),
    [
        ("github", "git@github.com:acme/service.git", "https://gitlab.com"),
        ("github", "ssh://git@github.com/acme/service.git", "https://gitlab.com"),
        ("github", "ssh://git@ssh.github.com:443/acme/service.git", "https://gitlab.com"),
        ("github", "https://github.com/acme/service.git", "https://gitlab.com"),
        ("gitlab", "git@gitlab.example.com:team/service.git", "https://gitlab.example.com"),
    ],
)
def test_accepts_credential_free_provider_clone_urls(
    provider: str,
    clone_url: str,
    gitlab_base_url: str,
) -> None:
    validate_clone_url(provider, clone_url, gitlab_base_url)


@pytest.mark.parametrize(
    "clone_url",
    [
        "root@github.com:acme/service.git",
        "git@evil.example:acme/service.git",
        "ssh://git:password@github.com/acme/service.git",
        "git@github.com:../service.git",
        "git@github.com:acme/service name.git",
        "https://github.com/acme/service.git?credential=unexpected",
        "file:///tmp/repository.git",
    ],
)
def test_rejects_untrusted_ssh_clone_urls(clone_url: str) -> None:
    with pytest.raises(GitProviderError):
        validate_clone_url("github", clone_url, "https://gitlab.com")


def test_git_environment_is_noninteractive_and_uses_strict_host_keys(tmp_path: Path) -> None:
    manager = GitWorkspaceManager(Settings(_env_file=None, workspace_root=tmp_path))

    env = manager._git_env("github")

    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert "BatchMode=yes" in env["GIT_SSH_COMMAND"]
    assert "StrictHostKeyChecking=yes" in env["GIT_SSH_COMMAND"]


def test_accepts_matching_manual_pull_request_url() -> None:
    validate_pull_request_url(
        "github",
        "acme/service",
        42,
        "https://github.com/acme/service/pull/42",
        "https://gitlab.com",
    )
    validate_pull_request_url(
        "gitlab",
        "team/service",
        7,
        "https://gitlab.example.com/team/service/-/merge_requests/7",
        "https://gitlab.example.com",
    )


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.example/acme/service/pull/42",
        "https://github.com/acme/other/pull/42",
        "https://github.com/acme/service/pull/41",
        "https://user:token@github.com/acme/service/pull/42",
        "https://github.com/acme/service/pull/42?token=secret",
    ],
)
def test_rejects_mismatched_manual_pull_request_url(url: str) -> None:
    with pytest.raises(GitProviderError):
        validate_pull_request_url("github", "acme/service", 42, url, "https://gitlab.com")
