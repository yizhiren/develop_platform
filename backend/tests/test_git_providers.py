import hashlib
import hmac
import json

import httpx
import pytest

from app.providers.github import GitHubProvider
from app.providers.gitlab import GitLabProvider


@pytest.mark.asyncio
async def test_github_contract_create_branch_and_verify_webhook() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer token"
        assert request.url.path == "/repos/acme/api/git/refs"
        return httpx.Response(201, json={"object": {"sha": "abc123"}})

    provider = GitHubProvider("token", "secret", transport=httpx.MockTransport(handler))
    assert await provider.create_branch("acme/api", "req/1", "base") == "abc123"
    body = json.dumps({"action": "push"}).encode()
    signature = "sha256=" + hmac.new(b"secret", body, hashlib.sha256).hexdigest()
    assert provider.verify_webhook(body, {"x-hub-signature-256": signature})
    await provider.close()


@pytest.mark.asyncio
async def test_gitlab_contract_create_branch_and_verify_webhook() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer token"
        assert request.url.path == "/api/v4/projects/acme/api/repository/branches"
        assert dict(request.url.params) == {"branch": "req/1", "ref": "base"}
        return httpx.Response(201, json={"commit": {"id": "abc123"}})

    provider = GitLabProvider("token", "secret", transport=httpx.MockTransport(handler))
    assert await provider.create_branch("acme/api", "req/1", "base") == "abc123"
    assert provider.verify_webhook(b"{}", {"x-gitlab-token": "secret"})
    await provider.close()


@pytest.mark.asyncio
async def test_github_pr_lookup_is_owner_scoped_and_checks_include_commit_statuses() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/repos/acme/api/pulls" and request.method == "GET":
            assert dict(request.url.params)["head"] == "acme:forgeflow/req-1"
            return httpx.Response(200, json=[{"number": 7}])
        if request.url.path == "/repos/acme/api/pulls/7":
            return httpx.Response(200, json={"number": 7, "html_url": "https://example/pr/7", "head": {"sha": "abc"}, "state": "open"})
        if request.url.path.endswith("/check-runs"):
            return httpx.Response(200, json={"check_runs": [{"id": 1, "name": "unit", "status": "completed", "conclusion": "success"}]})
        if request.url.path.endswith("/status"):
            return httpx.Response(200, json={"statuses": [{"id": 2, "context": "security", "state": "success", "target_url": "https://example/check"}]})
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    provider = GitHubProvider("token", transport=httpx.MockTransport(handler))
    pull_request = await provider.create_or_update_pull_request(
        "acme/api", "forgeflow/req-1", "main", "title", "body"
    )
    checks = await provider.get_checks("acme/api", "abc")
    assert pull_request.number == 7
    assert [item["name"] for item in checks] == ["unit", "security"]
    await provider.close()
