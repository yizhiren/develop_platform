import hashlib
import hmac
import json

import httpx
import pytest

from app.providers.git import GitProviderError
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


@pytest.mark.asyncio
async def test_github_error_preserves_safe_structured_validation_detail() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            422,
            json={
                "message": "Validation Failed",
                "errors": [
                    {
                        "resource": "PullRequest",
                        "code": "custom",
                        "message": "not all refs are readable",
                    }
                ],
            },
        )

    provider = GitHubProvider("token", transport=httpx.MockTransport(handler))
    with pytest.raises(GitProviderError, match="not all refs are readable"):
        await provider.create_or_update_pull_request(
            "acme/api", "huaban/req-1", "main", "title", "body"
        )
    await provider.close()


@pytest.mark.asyncio
async def test_github_checks_fall_back_to_provider_enforced_merge_gate() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/check-runs"):
            return httpx.Response(
                403,
                json={"message": "Resource not accessible by personal access token"},
            )
        if request.url.path.endswith("/status"):
            return httpx.Response(200, json={"state": "pending", "statuses": []})
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    provider = GitHubProvider("token", transport=httpx.MockTransport(handler))
    checks = await provider.get_checks("acme/api", "abc")
    assert checks == [
        {
            "id": "github-check-runs-permission",
            "name": "GitHub required checks (validated by merge gate)",
            "status": "completed",
            "conclusion": "neutral",
            "url": None,
            "warning": "check-runs API unavailable; PR clean state and GitHub branch protection will be enforced",
        }
    ]
    await provider.close()


@pytest.mark.asyncio
async def test_github_merge_requires_clean_pr_at_reviewed_head() -> None:
    requests: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, request.url.path))
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "state": "open",
                    "mergeable": True,
                    "mergeable_state": "clean",
                    "head": {"sha": "abc1234"},
                },
            )
        return httpx.Response(200, json={"merged": True, "sha": "merged123"})

    provider = GitHubProvider("token", transport=httpx.MockTransport(handler))
    assert await provider.merge("acme/api", 7, "abc1234") == "merged123"
    assert requests == [
        ("GET", "/repos/acme/api/pulls/7"),
        ("PUT", "/repos/acme/api/pulls/7/merge"),
    ]
    await provider.close()


@pytest.mark.asyncio
async def test_github_merge_rejects_changed_head_before_write() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        return httpx.Response(
            200,
            json={
                "state": "open",
                "mergeable": True,
                "mergeable_state": "clean",
                "head": {"sha": "changed5678"},
            },
        )

    provider = GitHubProvider("token", transport=httpx.MockTransport(handler))
    with pytest.raises(GitProviderError, match="reviewed commit"):
        await provider.merge("acme/api", 7, "abc1234")
    await provider.close()
