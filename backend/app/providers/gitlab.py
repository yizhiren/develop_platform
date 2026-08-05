import hmac
from typing import Any
from urllib.parse import quote

import httpx

from .git import GitProvider, GitProviderError, PullRequestRef


class GitLabProvider(GitProvider):
    def __init__(
        self,
        token: str,
        webhook_secret: str = "",
        base_url: str = "https://gitlab.com/api/v4",
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.webhook_secret = webhook_secret
        self.client = httpx.AsyncClient(
            base_url=base_url,
            transport=transport,
            timeout=30,
            headers={"Authorization": f"Bearer {token}"},
        )

    async def close(self) -> None:
        await self.client.aclose()

    async def get_repository(self, external_id: str) -> dict[str, Any]:
        return await self._json("GET", f"/projects/{quote(external_id, safe='')}")

    async def create_branch(self, repository: str, branch: str, base_sha: str) -> str:
        data = await self._json(
            "POST", f"/projects/{quote(repository, safe='')}/repository/branches", params={"branch": branch, "ref": base_sha}
        )
        return str(data["commit"]["id"])

    async def create_or_update_pull_request(
        self, repository: str, head: str, base: str, title: str, body: str
    ) -> PullRequestRef:
        project = quote(repository, safe="")
        existing = await self._json(
            "GET", f"/projects/{project}/merge_requests", params={"state": "opened", "source_branch": head, "target_branch": base}
        )
        if existing:
            iid = int(existing[0]["iid"])
            data = await self._json(
                "PUT", f"/projects/{project}/merge_requests/{iid}", json={"title": title, "description": body}
            )
        else:
            data = await self._json(
                "POST", f"/projects/{project}/merge_requests", json={"source_branch": head, "target_branch": base, "title": title, "description": body}
            )
        return PullRequestRef(number=int(data["iid"]), url=str(data["web_url"]), head_sha=str(data["sha"]), state=str(data["state"]))

    async def get_checks(self, repository: str, sha: str) -> list[dict[str, Any]]:
        project = quote(repository, safe="")
        data = await self._json("GET", f"/projects/{project}/pipelines", params={"sha": sha})
        return [{"id": item["id"], "name": f"pipeline-{item['id']}", "status": item["status"], "conclusion": item["status"], "url": item.get("web_url")} for item in data]

    async def merge(self, repository: str, number: int, expected_head_sha: str) -> str:
        data = await self._json(
            "PUT", f"/projects/{quote(repository, safe='')}/merge_requests/{number}/merge", json={"sha": expected_head_sha, "squash": True}
        )
        if data.get("state") != "merged":
            raise GitProviderError("gitlab.merge_rejected", "merge request was not merged")
        return str(data.get("merge_commit_sha") or data.get("squash_commit_sha"))

    def verify_webhook(self, body: bytes, headers: dict[str, str]) -> bool:
        del body
        return bool(self.webhook_secret) and hmac.compare_digest(
            self.webhook_secret, headers.get("x-gitlab-token", "")
        )

    async def _json(self, method: str, url: str, **kwargs) -> Any:
        response = await self.client.request(method, url, **kwargs)
        if response.status_code >= 400:
            retryable = response.status_code in {429, 500, 502, 503, 504}
            raise GitProviderError(f"gitlab.http_{response.status_code}", _safe_message(response), retryable)
        return response.json()


def _safe_message(response: httpx.Response) -> str:
    try:
        payload = response.json()
        return str(payload.get("message") or payload.get("error") or f"GitLab HTTP {response.status_code}")
    except Exception:
        return f"GitLab HTTP {response.status_code}"
