import pytest

from app.git_worker import execute_task
from app.providers.git import GitProvider, GitProviderError, PullRequestRef


class StubProvider(GitProvider):
    def __init__(self, checks):
        self.checks = checks
        self.merge_calls = []
        self.pull_request_calls = []

    async def get_checks(self, repository, sha):
        return self.checks

    async def merge(self, repository, number, expected_head_sha):
        self.merge_calls.append((repository, number, expected_head_sha))
        return "merged123"

    async def get_repository(self, external_id):
        return {}

    async def create_branch(self, repository, branch, base_sha):
        return base_sha

    async def create_or_update_pull_request(self, repository, head, base, title, body):
        self.pull_request_calls.append((repository, head, base, title, body))
        return PullRequestRef(1, "https://example.invalid/pr/1", "abc1234", "open")

    def verify_webhook(self, body, headers):
        return True


def envelope():
    return {
        "task_id": "task-1",
        "task_type": "git.merge_next",
        "payload": {"context": {"provider": "github", "repository": "acme/api", "pull_request_number": 7, "head_sha": "abc1234"}},
    }


@pytest.mark.asyncio
async def test_git_worker_revalidates_checks_before_merge():
    provider = StubProvider([{"status": "completed", "conclusion": "success"}])
    result = await execute_task(envelope(), provider)
    assert result["output"]["merged_sha"] == "merged123"
    assert provider.merge_calls == [("acme/api", 7, "abc1234")]


@pytest.mark.asyncio
async def test_git_worker_rejects_pending_checks():
    provider = StubProvider([{"status": "in_progress", "conclusion": None}])
    with pytest.raises(GitProviderError, match="not green"):
        await execute_task(envelope(), provider)
    assert provider.merge_calls == []


@pytest.mark.asyncio
async def test_git_worker_creates_pull_request_for_reviewed_head():
    provider = StubProvider([])
    result = await execute_task(
        {
            "task_id": "task-pr",
            "task_type": "git.create_pull_request",
            "payload": {
                "context": {
                    "provider": "github",
                    "repository": "acme/api",
                    "requirement_id": "req-1",
                    "requirement_repository_id": "link-1",
                    "work_branch": "huaban/req-1",
                    "target_branch": "main",
                    "head_sha": "abc1234",
                    "title": "Feature",
                    "description": "Description",
                }
            },
        },
        provider,
    )

    assert result["output"]["pull_request_number"] == 1
    assert result["output"]["head_sha"] == "abc1234"
    assert provider.pull_request_calls[0][:4] == (
        "acme/api",
        "huaban/req-1",
        "main",
        "[画板] Feature",
    )
