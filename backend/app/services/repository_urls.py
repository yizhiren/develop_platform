from __future__ import annotations

import re
from urllib.parse import urlparse

from ..providers.git import GitProviderError


SCP_SSH_URL = re.compile(
    r"^(?P<username>[A-Za-z0-9._-]+)@(?P<hostname>[A-Za-z0-9.-]+):(?P<path>[^?#]+)$"
)
REPOSITORY_PATH = re.compile(r"^[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)+$")


def expected_provider_host(provider: str, gitlab_base_url: str) -> str:
    return "github.com" if provider == "github" else (urlparse(gitlab_base_url).hostname or "").lower()


def validate_clone_url(
    provider: str,
    clone_url: str,
    gitlab_base_url: str,
    *,
    allow_local_git: bool = False,
) -> None:
    parsed = urlparse(clone_url)
    if parsed.query or parsed.fragment:
        raise GitProviderError("git.invalid_clone_url", "clone URL must not contain query or fragment")
    if parsed.scheme == "file" and allow_local_git:
        return

    expected_host = expected_provider_host(provider, gitlab_base_url)
    scp_match = SCP_SSH_URL.fullmatch(clone_url)
    if scp_match:
        _validate_ssh_parts(
            provider,
            scp_match.group("username"),
            scp_match.group("hostname"),
            scp_match.group("path"),
            expected_host,
        )
        return

    if parsed.scheme == "https":
        if parsed.username or parsed.password or (parsed.hostname or "").lower() != expected_host:
            raise GitProviderError(
                "git.clone_host_denied",
                "clone host does not match provider configuration",
            )
        _validate_repository_path(parsed.path)
        return

    if parsed.scheme == "ssh":
        if parsed.password:
            raise GitProviderError("git.invalid_clone_url", "SSH clone URL must not contain a password")
        _validate_ssh_parts(
            provider,
            parsed.username or "",
            parsed.hostname or "",
            parsed.path,
            expected_host,
        )
        return

    raise GitProviderError(
        "git.invalid_clone_url",
        "clone URL must be credential-free HTTPS or SSH",
    )


def validate_pull_request_url(
    provider: str,
    full_name: str,
    number: int,
    pull_request_url: str,
    gitlab_base_url: str,
) -> None:
    parsed = urlparse(pull_request_url)
    expected_host = expected_provider_host(provider, gitlab_base_url)
    if (
        parsed.scheme != "https"
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or (parsed.hostname or "").lower() != expected_host
    ):
        raise GitProviderError("git.invalid_pull_request_url", "pull request URL does not match provider")
    if provider == "github":
        expected_path = f"/{full_name}/pull/{number}"
    else:
        base_path = urlparse(gitlab_base_url).path.rstrip("/")
        expected_path = f"{base_path}/{full_name}/-/merge_requests/{number}"
    if parsed.path.rstrip("/") != expected_path:
        raise GitProviderError("git.invalid_pull_request_url", "pull request URL does not match repository and number")


def pull_request_web_url(
    provider: str,
    full_name: str,
    number: int,
    gitlab_base_url: str,
) -> str:
    if provider == "github":
        return f"https://github.com/{full_name}/pull/{number}"
    return f"{gitlab_base_url.rstrip('/')}/{full_name}/-/merge_requests/{number}"


def _validate_ssh_parts(
    provider: str,
    username: str,
    hostname: str,
    path: str,
    expected_host: str,
) -> None:
    allowed_hosts = {expected_host}
    if provider == "github":
        allowed_hosts.add("ssh.github.com")
    if username != "git":
        raise GitProviderError("git.invalid_clone_url", "SSH clone URL must use the git user")
    if hostname.lower() not in allowed_hosts:
        raise GitProviderError("git.clone_host_denied", "clone host does not match provider configuration")
    _validate_repository_path(path)


def _validate_repository_path(path: str) -> None:
    normalized = path.lstrip("/")
    repository_path = normalized.removesuffix(".git")
    if (
        not REPOSITORY_PATH.fullmatch(repository_path)
        or any(segment in {".", ".."} for segment in repository_path.split("/"))
    ):
        raise GitProviderError("git.invalid_clone_url", "clone URL repository path is invalid")
