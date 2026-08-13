import hashlib
import hmac
from typing import Any

import httpx

from .git import GitProvider, GitProviderError, PullRequestRef


class GitHubProvider(GitProvider):
    def __init__(
        self,
        token: str,
        webhook_secret: str = "",
        base_url: str = "https://api.github.com",
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.token = token
        self.webhook_secret = webhook_secret
        self.client = httpx.AsyncClient(
            base_url=base_url,
            transport=transport,
            timeout=30,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )

    async def close(self) -> None:
        await self.client.aclose()

    async def get_repository(self, external_id: str) -> dict[str, Any]:
        path = f"/repositories/{external_id}" if external_id.isdigit() else f"/repos/{external_id}"
        return await self._json("GET", path)

    async def create_branch(self, repository: str, branch: str, base_sha: str) -> str:
        data = await self._json(
            "POST", f"/repos/{repository}/git/refs", json={"ref": f"refs/heads/{branch}", "sha": base_sha}
        )
        return str(data["object"]["sha"])

    async def create_or_update_pull_request(
        self, repository: str, head: str, base: str, title: str, body: str
    ) -> PullRequestRef:
        owner = repository.split("/", 1)[0]
        existing = await self._json(
            "GET", f"/repos/{repository}/pulls", params={"state": "open", "head": f"{owner}:{head}", "base": base}
        )
        if existing:
            number = int(existing[0]["number"])
            data = await self._json(
                "PATCH", f"/repos/{repository}/pulls/{number}", json={"title": title, "body": body}
            )
        else:
            data = await self._json(
                "POST", f"/repos/{repository}/pulls", json={"head": head, "base": base, "title": title, "body": body, "draft": False}
            )
        return PullRequestRef(number=int(data["number"]), url=str(data["html_url"]), head_sha=str(data["head"]["sha"]), state=str(data["state"]))

    async def get_checks(self, repository: str, sha: str) -> list[dict[str, Any]]:
        check_runs_response = await self.client.get(
            f"/repos/{repository}/commits/{sha}/check-runs"
        )
        checks_accessible = check_runs_response.status_code != 403
        if checks_accessible:
            if check_runs_response.status_code >= 400:
                retryable = check_runs_response.status_code in {429, 500, 502, 503, 504}
                raise GitProviderError(
                    f"github.http_{check_runs_response.status_code}",
                    _safe_message(check_runs_response),
                    retryable,
                )
            check_runs = check_runs_response.json()
        else:
            check_runs = {"check_runs": []}
        combined_status = await self._json("GET", f"/repos/{repository}/commits/{sha}/status")
        checks = [
            {"id": item["id"], "name": item["name"], "status": item["status"], "conclusion": item.get("conclusion"), "url": item.get("html_url")}
            for item in check_runs.get("check_runs", [])
        ]
        checks.extend(
            {
                "id": f"status:{item.get('id', item.get('context', 'unknown'))}",
                "name": item.get("context", "commit-status"),
                "status": item.get("state"),
                "conclusion": item.get("state"),
                "url": item.get("target_url"),
            }
            for item in combined_status.get("statuses", [])
        )
        if not checks_accessible:
            checks.append(
                {
                    "id": "github-check-runs-permission",
                    "name": "GitHub required checks (validated by merge gate)",
                    "status": "completed",
                    "conclusion": "neutral",
                    "url": None,
                    "warning": "check-runs API unavailable; PR clean state and GitHub branch protection will be enforced",
                }
            )
        return checks

    async def merge(self, repository: str, number: int, expected_head_sha: str) -> str:
        pull_request = await self._json("GET", f"/repos/{repository}/pulls/{number}")
        actual_head_sha = str((pull_request.get("head") or {}).get("sha") or "")
        if actual_head_sha.lower() != expected_head_sha.lower():
            raise GitProviderError(
                "github.pull_request_head_mismatch",
                "pull request head no longer matches the reviewed commit",
            )
        mergeable = pull_request.get("mergeable")
        mergeable_state = str(pull_request.get("mergeable_state") or "unknown")
        if mergeable is None:
            raise GitProviderError(
                "github.mergeability_pending",
                "GitHub is still calculating pull request mergeability",
                True,
            )
        if pull_request.get("state") != "open" or mergeable is not True or mergeable_state != "clean":
            raise GitProviderError(
                "github.merge_gate_not_clean",
                f"pull request is not cleanly mergeable (state={mergeable_state})",
                True,
            )
        data = await self._json(
            "PUT", f"/repos/{repository}/pulls/{number}/merge", json={"sha": expected_head_sha, "merge_method": "squash"}
        )
        if not data.get("merged"):
            raise GitProviderError("github.merge_rejected", str(data.get("message", "merge rejected")))
        return str(data["sha"])

    def verify_webhook(self, body: bytes, headers: dict[str, str]) -> bool:
        if not self.webhook_secret:
            return False
        signature = headers.get("x-hub-signature-256", "")
        expected = "sha256=" + hmac.new(self.webhook_secret.encode(), body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(signature, expected)

    async def _json(self, method: str, url: str, **kwargs) -> Any:
        response = await self.client.request(method, url, **kwargs)
        if response.status_code >= 400:
            retryable = response.status_code in {429, 500, 502, 503, 504}
            raise GitProviderError(f"github.http_{response.status_code}", _safe_message(response), retryable)
        return response.json()


def _safe_message(response: httpx.Response) -> str:
    try:
        payload = response.json()
        message = str(payload.get("message", f"GitHub HTTP {response.status_code}"))
        details: list[str] = []
        for item in payload.get("errors") or []:
            if isinstance(item, dict):
                detail = item.get("message") or item.get("code")
            else:
                detail = item
            if detail and str(detail) not in details:
                details.append(str(detail))
        return f"{message}: {'; '.join(details)}" if details else message
    except Exception:
        return f"GitHub HTTP {response.status_code}"
