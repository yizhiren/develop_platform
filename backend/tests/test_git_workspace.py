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


def prepare_local_repository(tmp_path: Path) -> tuple[Path, Path]:
    source = tmp_path / "source"
    remote = tmp_path / "remote.git"
    source.mkdir()
    git("init", "-b", "main", cwd=source)
    git("config", "user.name", "Test", cwd=source)
    git("config", "user.email", "test@example.com", cwd=source)
    (source / "value.txt").write_text("before\n")
    (source / "generated.js").write_text("generated before\n")
    (source / ".gitignore").write_text("node_modules/\n")
    git("add", "value.txt", "generated.js", ".gitignore", cwd=source)
    git("commit", "-m", "initial", cwd=source)
    git("clone", "--bare", str(source), str(remote), cwd=tmp_path)
    return source, remote


@pytest.mark.asyncio
async def test_workspace_commit_locally_then_publish_reviewed_sha(tmp_path: Path) -> None:
    source = tmp_path / "source"
    remote = tmp_path / "remote.git"
    source.mkdir()
    git("init", "-b", "main", cwd=source)
    git("config", "user.name", "Test", cwd=source)
    git("config", "user.email", "test@example.com", cwd=source)
    (source / "value.txt").write_text("before\n")
    (source / "README.md").write_text("# Calculator service\n")
    (source / ".github" / "workflows").mkdir(parents=True)
    (source / ".github" / "workflows" / "ci.yml").write_text(
        "name: CI\non: [pull_request]\njobs: {}\n"
    )
    (source / "scripts" / "pipeline").mkdir(parents=True)
    (source / "scripts" / "pipeline" / "selftest.ts").write_text(
        "export const selftest = true\n"
    )
    git("add", "value.txt", "README.md", ".github", "scripts", cwd=source)
    git("commit", "-m", "initial", cwd=source)
    git("clone", "--bare", str(source), str(remote), cwd=tmp_path)

    settings = Settings(_env_file=None, workspace_root=tmp_path / "workspaces", allow_local_git=True)
    manager = GitWorkspaceManager(settings)
    context = {
        "requirement_id": "req-1",
        "title": "Change value",
        "description": "Fix scripts/pipeline/selftest.ts in CI",
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
    selected_files = {
        item["path"]: item
        for item in analysis["repositories"][0]["selected_files"]
    }
    assert selected_files["README.md"]["content"].startswith("# Calculator")
    assert selected_files[".github/workflows/ci.yml"]["selection_reason"] == "repository_baseline"
    assert selected_files["scripts/pipeline/selftest.ts"]["selection_reason"] == "requirement_reference"
    manifest = manager.prepare(context)
    checkout = Path(manifest["workspace_root"]) / "repo-1"
    (checkout / "value.txt").write_text("after\n")
    git("remote", "set-url", "origin", "file:///tmp/attacker.git", cwd=checkout)
    context["artifacts"]["workspace_manifest"] = manifest
    context["artifacts"]["development_report"] = {"summary": "Changed the value"}
    commit_result = manager.commit(context)
    assert commit_result["push_performed"] is False
    branch = manifest["repositories"][0]["work_branch"]
    assert subprocess.run(
        ["git", "rev-parse", "--verify", f"refs/heads/{branch}"],
        cwd=remote,
        capture_output=True,
    ).returncode != 0
    assert git("rev-parse", "HEAD", cwd=checkout) == commit_result["repositories"][0]["head_sha"]
    assert "+after" in commit_result["combined_diff"]
    changed_file = commit_result["repositories"][0]["changed_files"][0]
    assert changed_file["path"] == "value.txt"
    assert changed_file["content"] == "after\n"
    assert len(changed_file["sha256"]) == 64

    (checkout / "value.txt").write_text("failed agent residue\n")
    (checkout / "untracked.txt").write_text("temporary\n")
    restored = manager.restore(context)
    assert (checkout / "value.txt").read_text() == "after\n"
    assert not (checkout / "untracked.txt").exists()
    assert restored["restored"][0]["discarded_entries"] == 2

    context["artifacts"]["development_commit_manifest"] = commit_result
    ssh_only = await manager.publish(context, lambda _: None)
    assert ssh_only["repositories"][0]["publication_mode"] == "ssh_branch_only"
    assert ssh_only["repositories"][0]["pull_request_number"] is None
    assert "/compare/main...huaban%2Freq-req-1?expand=1" in ssh_only["repositories"][0]["pull_request_url"]
    assert git("rev-parse", f"refs/heads/{branch}", cwd=remote) == commit_result["repositories"][0]["head_sha"]

    result = await manager.publish(context, lambda _: StubPullRequestProvider())
    assert result["repositories"][0]["pull_request_number"] == 17
    assert git("rev-parse", f"refs/heads/{branch}", cwd=remote) == result["repositories"][0]["head_sha"]
    assert result["repositories"][0]["head_sha"] == commit_result["repositories"][0]["head_sha"]
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
        manager.commit(context)


def test_commit_skips_ignored_dependency_symlinks(tmp_path: Path) -> None:
    _, remote = prepare_local_repository(tmp_path)
    settings = Settings(_env_file=None, workspace_root=tmp_path / "workspaces", allow_local_git=True)
    manager = GitWorkspaceManager(settings)
    context = {
        "requirement_id": "req-ignored-cache",
        "title": "Change tracked source",
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
    manifest = manager.prepare(context)
    checkout = Path(manifest["workspace_root"]) / "repo-1"
    (checkout / "value.txt").write_text("after\n")
    dependency_bin = checkout / "node_modules" / ".bin"
    dependency_bin.mkdir(parents=True)
    (checkout / "node_modules" / "tool.js").write_text("module.exports = {}\n")
    (dependency_bin / "tool").symlink_to("../tool.js")
    context["artifacts"]["workspace_manifest"] = manifest
    context["artifacts"]["development_report"] = {"repositories_changed": ["repo-1"]}

    result = manager.commit(context)

    assert "+after" in result["combined_diff"]
    assert "node_modules" not in result["combined_diff"]


def test_commit_only_copies_agent_changed_files_not_validation_outputs(tmp_path: Path) -> None:
    _, remote = prepare_local_repository(tmp_path)
    settings = Settings(_env_file=None, workspace_root=tmp_path / "workspaces", allow_local_git=True)
    manager = GitWorkspaceManager(settings)
    context = {
        "requirement_id": "req-selected-files",
        "title": "Change tracked source",
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
    manifest = manager.prepare(context)
    checkout = Path(manifest["workspace_root"]) / "repo-1"
    (checkout / "value.txt").write_text("after\n")
    (checkout / "generated.js").write_text("validation output\n")
    context["artifacts"]["workspace_manifest"] = manifest
    context["artifacts"]["development_report"] = {
        "repositories_changed": ["repo-1"],
        "files_changed": ["repo-1/value.txt"],
    }

    result = manager.commit(context)

    assert "+after" in result["combined_diff"]
    assert "validation output" not in result["combined_diff"]
    assert (checkout / "generated.js").read_text() == "generated before\n"


def test_rework_commit_preserves_unpublished_prior_reviewed_commit(tmp_path: Path) -> None:
    _, remote = prepare_local_repository(tmp_path)
    settings = Settings(_env_file=None, workspace_root=tmp_path / "workspaces", allow_local_git=True)
    manager = GitWorkspaceManager(settings)
    context = {
        "requirement_id": "req-rework",
        "title": "Cumulative rework",
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
    manifest = manager.prepare(context)
    checkout = Path(manifest["workspace_root"]) / "repo-1"
    (checkout / "value.txt").write_text("first rework\n")
    (checkout / "generated.js").unlink()
    context["artifacts"]["workspace_manifest"] = manifest
    context["artifacts"]["development_report"] = {
        "repositories_changed": ["repo-1"],
        "files_changed": ["repo-1/value.txt", "repo-1/generated.js"],
    }

    first = manager.commit(context)
    first_head = first["repositories"][0]["head_sha"]
    assert git("rev-parse", "HEAD", cwd=checkout) == first_head
    assert subprocess.run(
        ["git", "rev-parse", "--verify", "refs/heads/huaban/req-req-rework"],
        cwd=remote,
        capture_output=True,
    ).returncode != 0

    context["artifacts"]["development_commit_manifest"] = first
    context["artifacts"]["development_report"] = {
        "repositories_changed": ["repo-1"],
        "files_changed": ["repo-1/regression.test.py"],
    }
    (checkout / "regression.test.py").write_text("assert True\n")

    second = manager.commit(context)
    second_head = second["repositories"][0]["head_sha"]

    assert git("rev-parse", f"{second_head}^", cwd=checkout) == first_head
    assert (checkout / "value.txt").read_text() == "first rework\n"
    assert not (checkout / "generated.js").exists()
    assert (checkout / "regression.test.py").read_text() == "assert True\n"
    assert "first rework" in second["combined_diff"]
    assert "generated.js" in second["combined_diff"]
    assert "regression.test.py" in second["combined_diff"]


def test_commit_rejects_nonignored_untracked_symlink(tmp_path: Path) -> None:
    _, remote = prepare_local_repository(tmp_path)
    settings = Settings(_env_file=None, workspace_root=tmp_path / "workspaces", allow_local_git=True)
    manager = GitWorkspaceManager(settings)
    context = {
        "requirement_id": "req-source-symlink",
        "title": "Unsafe link",
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
    manifest = manager.prepare(context)
    checkout = Path(manifest["workspace_root"]) / "repo-1"
    (checkout / "unsafe-link").symlink_to("value.txt")
    context["artifacts"]["workspace_manifest"] = manifest
    context["artifacts"]["development_report"] = {"repositories_changed": ["repo-1"]}

    with pytest.raises(GitProviderError, match="symlink"):
        manager.commit(context)
