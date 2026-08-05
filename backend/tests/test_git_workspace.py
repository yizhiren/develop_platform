import subprocess
from pathlib import Path

import pytest

from app.core.config import Settings
from app.providers.git import GitProvider, GitProviderError, PullRequestRef
from app.services.git_workspace import GitWorkspaceManager


class StubPullRequestProvider(GitProvider):
    async def create_or_update_pull_request(self, repository, head, base, title, body):
        return PullRequestRef(17, "https://example.invalid/pr/17", "unused", "open")

    async def get_repository(self, external_id): return {}
    async def create_branch(self, repository, branch, base_sha): return base_sha
    async def get_checks(self, repository, sha): return []
    async def merge(self, repository, number, expected_head_sha): return expected_head_sha
    def verify_webhook(self, body, headers): return True


def git(*args: str, cwd: Path) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


@pytest.mark.asyncio
async def test_workspace_prepare_publish_and_push(tmp_path: Path) -> None:
    source = tmp_path / "source"
    remote = tmp_path / "remote.git"
    source.mkdir()
    git("init", "-b", "main", cwd=source)
    git("config", "user.name", "Test", cwd=source)
    git("config", "user.email", "test@example.com", cwd=source)
    (source / "value.txt").write_text("before\n")
    (source / "README.md").write_text("# Calculator service\n")
    git("add", "value.txt", "README.md", cwd=source)
    git("commit", "-m", "initial", cwd=source)
    git("clone", "--bare", str(source), str(remote), cwd=tmp_path)

    settings = Settings(_env_file=None, workspace_root=tmp_path / "workspaces", allow_local_git=True)
    manager = GitWorkspaceManager(settings)
    context = {
        "requirement_id": "req-1",
        "title": "Change value",
        "repositories": [{
            "requirement_repository_id": "link-1",
            "repository_id": "repo-1",
            "provider": "github",
            "full_name": "acme/repo",
            "clone_url": remote.as_uri(),
            "target_branch": "main",
        }],
        "artifacts": {},
    }
    analysis = manager.prepare_analysis(context)
    assert analysis["source"] == "trusted_read_only_checkout"
    assert "README.md" in analysis["repositories"][0]["file_tree"]
    assert analysis["repositories"][0]["selected_files"][0]["content"].startswith("# Calculator")
    manifest = manager.prepare(context)
    checkout = Path(manifest["workspace_root"]) / "repo-1"
    (checkout / "value.txt").write_text("after\n")
    git("remote", "set-url", "origin", "file:///tmp/attacker.git", cwd=checkout)
    context["artifacts"]["workspace_manifest"] = manifest
    context["artifacts"]["development_report"] = {"summary": "Changed the value"}
    result = await manager.publish(context, lambda _: StubPullRequestProvider())
    assert result["repositories"][0]["pull_request_number"] == 17
    branch = manifest["repositories"][0]["work_branch"]
    assert git("rev-parse", f"refs/heads/{branch}", cwd=remote) == result["repositories"][0]["head_sha"]
    assert "+after" in result["combined_diff"]
    context["artifacts"]["delivery_manifest"] = result
    verification = manager.prepare_verification(context)
    verification_checkout = Path(verification["workspace_root"]) / "repo-1"
    assert verification_checkout != checkout
    assert (verification_checkout / "value.txt").read_text() == "after\n"
    assert verification["checkout_type"] == "published_heads"
    context["repositories"][0]["status"] = "ready"
    incremental_before_merge = manager.prepare_verification(context, incremental=True)
    assert incremental_before_merge["checkout_type"] == "incremental_combination"
    assert (Path(incremental_before_merge["workspace_root"]) / "repo-1" / "value.txt").read_text() == "after\n"

    git("push", "origin", f"{branch}:main", cwd=checkout)
    context["repositories"][0]["head_sha"] = result["repositories"][0]["head_sha"]
    context["repositories"][0]["status"] = "merged"
    incremental_after_merge = manager.prepare_verification(context, incremental=True)
    assert (Path(incremental_after_merge["workspace_root"]) / "repo-1" / "value.txt").read_text() == "after\n"
    final_verification = manager.prepare_verification(context, final=True)
    final_checkout = Path(final_verification["workspace_root"]) / "repo-1"
    assert (final_checkout / "value.txt").read_text() == "after\n"
    assert final_verification["checkout_type"] == "merged_targets"

    (checkout / "credentials.txt").write_text("api_key=abcdefghijklmnopqrstuvwx\n")
    with pytest.raises(GitProviderError, match="secret"):
        await manager.publish(context, lambda _: StubPullRequestProvider())
