from __future__ import annotations

import base64
import hashlib
import os
import re
import signal
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote
from ..core.config import Settings
from ..providers.git import GitProvider, GitProviderError
from .repository_urls import validate_clone_url


SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
SECRET_PATTERNS = [
    re.compile(r"(?i)(api[_-]?key|secret|password|token)\s*[:=]\s*['\"]?[A-Za-z0-9_./+=-]{12,}"),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
]
ANALYSIS_FILENAMES = {
    ".gitlab-ci.yml",
    "jenkinsfile",
    "makefile",
    "package.json",
    "pyproject.toml",
    "pytest.ini",
    "requirements.txt",
    "tox.ini",
    "go.mod",
    "cargo.toml",
    "pom.xml",
    "build.gradle",
    "dockerfile",
    "docker-compose.yml",
    "compose.yml",
    "main.py",
    "app.py",
    "index.ts",
    "index.tsx",
    "main.ts",
    "main.go",
}


class GitWorkspaceManager:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.root = settings.workspace_root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.leases = self.root / ".leases"
        self.leases.mkdir(parents=True, exist_ok=True)
        self.publishing = self.root / ".publishing"
        self.publishing.mkdir(parents=True, exist_ok=True)

    def prepare(self, context: dict[str, Any]) -> dict[str, Any]:
        requirement_id = _safe_id(context["requirement_id"])
        self.cleanup_stale()
        analysis_workspace_id = _safe_id(f"{requirement_id}-analysis")
        shutil.rmtree(self.root / analysis_workspace_id, ignore_errors=True)
        self._lease_path(analysis_workspace_id).unlink(missing_ok=True)
        requirement_root = self.root / requirement_id
        requirement_root.mkdir(parents=True, exist_ok=True)
        manifests: list[dict[str, Any]] = []
        for repository in context.get("repositories", []):
            repository_id = _safe_id(repository["repository_id"])
            target = requirement_root / repository_id
            if target.exists():
                shutil.rmtree(target)
            clone_url = str(repository["clone_url"])
            self._validate_clone_url(repository["provider"], clone_url)
            env = self._git_env(repository["provider"])
            self._run(
                ["git", "clone", "--no-tags", "--single-branch", "--branch", repository["target_branch"], "--", clone_url, str(target)],
                self.root,
                env,
                timeout_seconds=900,
            )
            work_branch = f"huaban/req-{requirement_id[:12]}"
            self._run(["git", "config", "core.hooksPath", "/dev/null"], target, env)
            self._run(["git", "config", "user.name", "画板 Developer Agent"], target, env)
            self._run(["git", "config", "user.email", "agent@huaban.local"], target, env)
            self._run(["git", "checkout", "-B", work_branch], target, env)
            baseline_sha = self._run(["git", "rev-parse", "HEAD"], target, env).strip()
            manifests.append(
                {
                    "requirement_repository_id": repository["requirement_repository_id"],
                    "repository_id": repository_id,
                    "provider": repository["provider"],
                    "full_name": repository["full_name"],
                    "target_branch": repository["target_branch"],
                    "work_branch": work_branch,
                    "baseline_sha": baseline_sha,
                    "relative_path": repository_id,
                }
            )
        self._touch_lease(requirement_id)
        if _directory_size(self.root, excluded={self.leases, self.publishing}) > self.settings.workspace_max_bytes:
            shutil.rmtree(requirement_root, ignore_errors=True)
            self._lease_path(requirement_id).unlink(missing_ok=True)
            raise GitProviderError("git.workspace_quota_exceeded", "workspace storage quota exceeded")
        return {
            "schema_version": "1.0",
            "workspace_root": str(requirement_root),
            "repositories": manifests,
        }

    def restore(self, context: dict[str, Any]) -> dict[str, Any]:
        """Discard failed-run residue while preserving the latest local commit."""
        manifest = context.get("artifacts", {}).get("workspace_manifest")
        if not manifest or not manifest.get("workspace_root"):
            raise GitProviderError("git.workspace_missing", "workspace manifest is missing")
        root = Path(manifest["workspace_root"]).resolve()
        if not root.is_relative_to(self.root):
            raise GitProviderError("git.workspace_escape", "workspace is outside configured root")
        restored: list[dict[str, Any]] = []
        for item in manifest.get("repositories", []):
            repository_root = (root / item["relative_path"]).resolve()
            if not repository_root.is_relative_to(root) or not (repository_root / ".git").exists():
                raise GitProviderError("git.workspace_missing", "repository workspace is missing")
            env = self._git_env(item["provider"])
            before = self._run(["git", "status", "--porcelain=v1"], repository_root, env)
            self._run(["git", "reset", "--hard", "HEAD"], repository_root, env)
            self._run(["git", "clean", "-fd"], repository_root, env)
            restored.append(
                {
                    "repository_id": item["repository_id"],
                    "head_sha": self._run(["git", "rev-parse", "HEAD"], repository_root, env).strip(),
                    "discarded_entries": len([line for line in before.splitlines() if line.strip()]),
                }
            )
        return {**manifest, "restored": restored}

    def prepare_analysis(self, context: dict[str, Any]) -> dict[str, Any]:
        requirement_id = _safe_id(context["requirement_id"])
        workspace_id = _safe_id(f"{requirement_id}-analysis")
        self.cleanup_stale()
        analysis_root = self.root / workspace_id
        shutil.rmtree(analysis_root, ignore_errors=True)
        analysis_root.mkdir(parents=True)
        snapshots: list[dict[str, Any]] = []
        remaining_content_bytes = 100_000
        path_hints = _analysis_path_hints(context)
        try:
            for repository in context.get("repositories", []):
                repository_id = _safe_id(repository["repository_id"])
                clone_url = str(repository["clone_url"])
                self._validate_clone_url(repository["provider"], clone_url)
                target = analysis_root / repository_id
                env = self._git_env(repository["provider"])
                self._run(
                    ["git", "clone", "--no-tags", "--single-branch", "--branch", repository["target_branch"], "--", clone_url, str(target)],
                    self.root,
                    env,
                    timeout_seconds=900,
                )
                self._run(["git", "config", "core.hooksPath", "/dev/null"], target, env)
                head_sha = self._run(["git", "rev-parse", "HEAD"], target, env).strip()
                snapshot, used = _repository_analysis_snapshot(
                    target,
                    remaining_content_bytes,
                    path_hints,
                )
                remaining_content_bytes -= used
                snapshots.append(
                    {
                        "requirement_repository_id": repository["requirement_repository_id"],
                        "repository_id": repository_id,
                        "provider": repository["provider"],
                        "full_name": repository["full_name"],
                        "target_branch": repository["target_branch"],
                        "head_sha": head_sha,
                        "relative_path": repository_id,
                        **snapshot,
                    }
                )
            self._touch_lease(workspace_id)
            if _directory_size(self.root, excluded={self.leases, self.publishing}) > self.settings.workspace_max_bytes:
                raise GitProviderError("git.workspace_quota_exceeded", "workspace storage quota exceeded")
        except Exception:
            shutil.rmtree(analysis_root, ignore_errors=True)
            self._lease_path(workspace_id).unlink(missing_ok=True)
            raise
        return {
            "schema_version": "1.0",
            "source": "trusted_read_only_checkout",
            "workspace_root": str(analysis_root),
            "repositories": snapshots,
        }

    def prepare_verification(
        self,
        context: dict[str, Any],
        final: bool = False,
        incremental: bool = False,
    ) -> dict[str, Any]:
        requirement_id = _safe_id(context["requirement_id"])
        suffix = "final-verify" if final else "incremental-verify" if incremental else "verify"
        workspace_id = _safe_id(f"{requirement_id}-{suffix}")
        self.cleanup_stale()
        verification_root = self.root / workspace_id
        if verification_root.exists():
            shutil.rmtree(verification_root)
        verification_root.mkdir(parents=True)
        delivery_by_repository = {
            item["repository_id"]: item
            for item in context.get("artifacts", {}).get("delivery_manifest", {}).get("repositories", [])
        }
        manifests: list[dict[str, Any]] = []
        try:
            for repository in context.get("repositories", []):
                repository_id = _safe_id(repository["repository_id"])
                delivery = delivery_by_repository.get(repository_id, {})
                use_merged_target = final or (incremental and repository.get("status") == "merged")
                branch = repository["target_branch"] if use_merged_target else delivery.get("work_branch")
                expected_sha = repository.get("head_sha") if use_merged_target else delivery.get("head_sha")
                if not branch or not expected_sha:
                    raise GitProviderError(
                        "git.verification_ref_missing",
                        "verification branch or expected head SHA is missing",
                    )
                clone_url = str(repository["clone_url"])
                self._validate_clone_url(repository["provider"], clone_url)
                target = verification_root / repository_id
                env = self._git_env(repository["provider"])
                self._run(
                    [
                        "git",
                        "clone",
                        "--depth",
                        "1",
                        "--no-tags",
                        "--single-branch",
                        "--branch",
                        branch,
                        "--",
                        clone_url,
                        str(target),
                    ],
                    self.root,
                    env,
                    timeout_seconds=900,
                )
                self._run(["git", "config", "core.hooksPath", "/dev/null"], target, env)
                actual_sha = self._run(["git", "rev-parse", "HEAD"], target, env).strip()
                if actual_sha != expected_sha:
                    raise GitProviderError(
                        "git.verification_head_mismatch",
                        "fresh verification checkout does not match the expected head SHA",
                    )
                manifests.append(
                    {
                        "requirement_repository_id": repository["requirement_repository_id"],
                        "repository_id": repository_id,
                        "provider": repository["provider"],
                        "full_name": repository["full_name"],
                        "branch": branch,
                        "head_sha": actual_sha,
                        "relative_path": repository_id,
                    }
                )
            self._touch_lease(workspace_id)
            if _directory_size(self.root, excluded={self.leases, self.publishing}) > self.settings.workspace_max_bytes:
                raise GitProviderError("git.workspace_quota_exceeded", "workspace storage quota exceeded")
        except Exception:
            shutil.rmtree(verification_root, ignore_errors=True)
            self._lease_path(workspace_id).unlink(missing_ok=True)
            raise
        return {
            "schema_version": "1.0",
            "checkout_type": (
                "merged_targets" if final else "incremental_combination" if incremental else "published_heads"
            ),
            "workspace_root": str(verification_root),
            "repositories": manifests,
        }

    async def publish(
        self,
        context: dict[str, Any],
        provider_factory: Callable[[str], GitProvider | None],
    ) -> dict[str, Any]:
        committed = context.get("artifacts", {}).get("development_commit_manifest")
        if committed:
            return await self._publish_committed(context, committed, provider_factory)
        manifest = context.get("artifacts", {}).get("workspace_manifest")
        if not manifest:
            raise GitProviderError("git.workspace_missing", "workspace manifest is missing")
        root = Path(manifest["workspace_root"]).resolve()
        if not root.is_relative_to(self.root):
            raise GitProviderError("git.workspace_escape", "workspace is outside configured root")
        requirement_id = _safe_id(context["requirement_id"])
        self._touch_lease(requirement_id)
        published: list[dict[str, Any]] = []
        combined_diff: list[str] = []
        repository_context = {item["repository_id"]: item for item in context.get("repositories", [])}
        previous_delivery = {
            item["repository_id"]: item
            for item in context.get("artifacts", {}).get("delivery_manifest", {}).get("repositories", [])
        }
        for item in manifest.get("repositories", []):
            repo_context = repository_context[item["repository_id"]]
            repository_root = (root / item["relative_path"]).resolve()
            if not repository_root.is_relative_to(root):
                raise GitProviderError("git.workspace_escape", "repository path escapes workspace")
            env = self._git_env(item["provider"])
            try:
                trusted_root = self._trusted_publish_checkout(
                    requirement_id,
                    repo_context,
                    item,
                    repository_root,
                    env,
                    _repository_changed_paths(context, item),
                )
                status = self._run(["git", "status", "--porcelain=v1"], trusted_root, env)
                head_sha = self._run(["git", "rev-parse", "HEAD"], trusted_root, env).strip()
                if status.strip():
                    self._run(["git", "add", "-A"], trusted_root, env)
                    diff_for_scan = self._run(
                        ["git", "diff", "--cached", "--no-ext-diff", "--binary"],
                        trusted_root,
                        env,
                        max_bytes=2_000_000,
                    )
                    self._scan_secrets(diff_for_scan)
                    self._run(["git", "commit", "-m", f"feat: {context['title']}"], trusted_root, env)
                    head_sha = self._run(["git", "rev-parse", "HEAD"], trusted_root, env).strip()
                    self._run(
                        ["git", "push", "--force-with-lease", "origin", f"HEAD:refs/heads/{item['work_branch']}"],
                        trusted_root,
                        env,
                    )
                elif (
                    head_sha == item["baseline_sha"]
                    or previous_delivery.get(item["repository_id"], {}).get("head_sha") == head_sha
                ):
                    shutil.rmtree(trusted_root, ignore_errors=True)
                    continue

                # Replace all Agent-touched Git metadata with the trusted clone before any retry.
                shutil.rmtree(repository_root)
                shutil.move(str(trusted_root), str(repository_root))
                pull_request = await _create_pull_request_or_manual_link(provider_factory, item, context)
                review_diff = self._run(
                    ["git", "diff", "--no-ext-diff", f"{item['baseline_sha']}..{head_sha}"],
                    repository_root,
                    env,
                    max_bytes=500_000,
                )
            except Exception:
                pending = self.publishing / requirement_id / item["repository_id"]
                shutil.rmtree(pending, ignore_errors=True)
                raise
            combined_diff.append(f"## {item['full_name']}\n{review_diff}")
            published.append(
                {
                    "requirement_repository_id": item["requirement_repository_id"],
                    "repository_id": item["repository_id"],
                    "work_branch": item["work_branch"],
                    "baseline_sha": item["baseline_sha"],
                    "head_sha": head_sha,
                    "pull_request_number": pull_request["number"],
                    "pull_request_url": pull_request["url"],
                    "publication_mode": pull_request["mode"],
                }
            )
        if not published:
            raise GitProviderError("git.no_changes", "developer agent produced no repository changes")
        self._touch_lease(requirement_id)
        return {
            "schema_version": "1.0",
            "repositories": published,
            "combined_diff": "\n\n".join(combined_diff),
        }

    def commit(self, context: dict[str, Any]) -> dict[str, Any]:
        """Create trusted local commits from the developer worktree without pushing."""
        manifest = context.get("artifacts", {}).get("workspace_manifest")
        if not manifest:
            raise GitProviderError("git.workspace_missing", "workspace manifest is missing")
        root = Path(manifest["workspace_root"]).resolve()
        if not root.is_relative_to(self.root):
            raise GitProviderError("git.workspace_escape", "workspace is outside configured root")
        requirement_id = _safe_id(context["requirement_id"])
        self._touch_lease(requirement_id)
        repository_context = {item["repository_id"]: item for item in context.get("repositories", [])}
        changed_repository_ids = set(
            context.get("artifacts", {}).get("development_report", {}).get("repositories_changed", [])
        )
        committed: list[dict[str, Any]] = []
        combined_diff: list[str] = []
        for item in manifest.get("repositories", []):
            if changed_repository_ids and item["repository_id"] not in changed_repository_ids:
                continue
            repo_context = repository_context[item["repository_id"]]
            repository_root = (root / item["relative_path"]).resolve()
            if not repository_root.is_relative_to(root):
                raise GitProviderError("git.workspace_escape", "repository path escapes workspace")
            env = self._git_env(item["provider"])
            try:
                trusted_root = self._trusted_publish_checkout(
                    requirement_id,
                    repo_context,
                    item,
                    repository_root,
                    env,
                    _repository_changed_paths(context, item),
                    trusted_base=_repository_committed_state(context, item),
                )
                status = self._run(["git", "status", "--porcelain=v1"], trusted_root, env)
                if not status.strip():
                    shutil.rmtree(trusted_root, ignore_errors=True)
                    continue
                self._run(["git", "add", "-A"], trusted_root, env)
                staged_diff = self._run(
                    ["git", "diff", "--cached", "--no-ext-diff", "--binary"],
                    trusted_root,
                    env,
                    max_bytes=2_000_000,
                )
                self._scan_secrets(staged_diff)
                self._run(["git", "commit", "-m", f"feat: {context['title']}"], trusted_root, env)
                head_sha = self._run(["git", "rev-parse", "HEAD"], trusted_root, env).strip()
                review_diff = self._run(
                    ["git", "diff", "--no-ext-diff", f"{item['baseline_sha']}..{head_sha}"],
                    trusted_root,
                    env,
                    max_bytes=500_000,
                )
                changed_files = self._review_file_snapshots(
                    trusted_root,
                    item["baseline_sha"],
                    head_sha,
                    env,
                )
                shutil.rmtree(repository_root)
                shutil.move(str(trusted_root), str(repository_root))
            except Exception:
                pending = self.publishing / requirement_id / item["repository_id"]
                shutil.rmtree(pending, ignore_errors=True)
                raise
            combined_diff.append(f"## {item['full_name']}\n{review_diff}")
            committed.append(
                {
                    "requirement_repository_id": item["requirement_repository_id"],
                    "repository_id": item["repository_id"],
                    "full_name": item["full_name"],
                    "work_branch": item["work_branch"],
                    "baseline_sha": item["baseline_sha"],
                    "head_sha": head_sha,
                    "diff_sha256": hashlib.sha256(review_diff.encode()).hexdigest(),
                    "changed_files": changed_files,
                }
            )
        if not committed:
            raise GitProviderError("git.no_changes", "developer agent produced no repository changes")
        self._touch_lease(requirement_id)
        return {
            "schema_version": "1.0",
            "summary": "Developer changes committed locally; nothing was pushed.",
            "push_performed": False,
            "repositories": committed,
            "combined_diff": "\n\n".join(combined_diff),
        }

    def _review_file_snapshots(
        self,
        repository_root: Path,
        baseline_sha: str,
        head_sha: str,
        env: dict[str, str],
    ) -> list[dict[str, Any]]:
        """Capture bounded post-commit text for files changed in the reviewed SHA."""
        names = self._run(
            ["git", "diff", "--name-only", "--diff-filter=ACMR", f"{baseline_sha}..{head_sha}"],
            repository_root,
            env,
            max_bytes=100_000,
        )
        snapshots: list[dict[str, Any]] = []
        remaining = 250_000
        for relative in names.splitlines():
            if not relative or remaining <= 0:
                break
            path = (repository_root / relative).resolve()
            if not path.is_relative_to(repository_root) or not path.is_file() or path.is_symlink():
                continue
            data = path.read_bytes()
            digest = hashlib.sha256(data).hexdigest()
            entry: dict[str, Any] = {
                "path": relative,
                "sha256": digest,
                "size_bytes": len(data),
            }
            if b"\x00" in data:
                entry["content_omitted"] = "binary"
            elif len(data) > 100_000 or len(data) > remaining:
                entry["content_omitted"] = "size_limit"
            else:
                content = data.decode("utf-8", errors="replace")
                if any(pattern.search(content) for pattern in SECRET_PATTERNS):
                    entry["content_omitted"] = "potential_secret"
                else:
                    entry["content"] = content
                    remaining -= len(data)
            snapshots.append(entry)
        return snapshots

    async def _publish_committed(
        self,
        context: dict[str, Any],
        committed: dict[str, Any],
        provider_factory: Callable[[str], GitProvider | None],
    ) -> dict[str, Any]:
        manifest = context.get("artifacts", {}).get("workspace_manifest")
        if not manifest:
            raise GitProviderError("git.workspace_missing", "workspace manifest is missing")
        root = Path(manifest["workspace_root"]).resolve()
        if not root.is_relative_to(self.root):
            raise GitProviderError("git.workspace_escape", "workspace is outside configured root")
        workspace_by_repository = {item["repository_id"]: item for item in manifest.get("repositories", [])}
        published: list[dict[str, Any]] = []
        combined_diff: list[str] = []
        for commit_item in committed.get("repositories", []):
            item = workspace_by_repository[commit_item["repository_id"]]
            repository_root = (root / item["relative_path"]).resolve()
            if not repository_root.is_relative_to(root):
                raise GitProviderError("git.workspace_escape", "repository path escapes workspace")
            env = self._git_env(item["provider"])
            if self._run(["git", "status", "--porcelain=v1"], repository_root, env).strip():
                raise GitProviderError("git.commit_dirty", "committed developer workspace changed after review")
            head_sha = self._run(["git", "rev-parse", "HEAD"], repository_root, env).strip()
            if head_sha != commit_item["head_sha"]:
                raise GitProviderError("git.commit_mismatch", "reviewed commit SHA no longer matches workspace HEAD")
            review_diff = self._run(
                ["git", "diff", "--no-ext-diff", f"{commit_item['baseline_sha']}..{head_sha}"],
                repository_root,
                env,
                max_bytes=500_000,
            )
            if hashlib.sha256(review_diff.encode()).hexdigest() != commit_item.get("diff_sha256"):
                raise GitProviderError("git.diff_mismatch", "reviewed diff no longer matches committed evidence")
            self._scan_secrets(review_diff)
            self._run(
                ["git", "push", "--force-with-lease", "origin", f"HEAD:refs/heads/{item['work_branch']}"],
                repository_root,
                env,
            )
            pull_request = await _create_pull_request_or_manual_link(provider_factory, item, context)
            combined_diff.append(f"## {item['full_name']}\n{review_diff}")
            published.append(
                {
                    "requirement_repository_id": item["requirement_repository_id"],
                    "repository_id": item["repository_id"],
                    "work_branch": item["work_branch"],
                    "baseline_sha": item["baseline_sha"],
                    "head_sha": head_sha,
                    "pull_request_number": pull_request["number"],
                    "pull_request_url": pull_request["url"],
                    "publication_mode": pull_request["mode"],
                }
            )
        if not published:
            raise GitProviderError("git.no_changes", "no reviewed commits are available to publish")
        return {
            "schema_version": "1.0",
            "summary": (
                "Reviewed branches pushed; pull requests created where API credentials were available. "
                "Tokenless providers expose a manual compare link."
            ),
            "repositories": published,
            "combined_diff": "\n\n".join(combined_diff),
        }

    def _trusted_publish_checkout(
        self,
        requirement_id: str,
        repository: dict[str, Any],
        manifest_item: dict[str, Any],
        developer_root: Path,
        env: dict[str, str],
        changed_paths: set[Path] | None = None,
        trusted_base: dict[str, Any] | None = None,
    ) -> Path:
        target = self.publishing / requirement_id / manifest_item["repository_id"]
        shutil.rmtree(target, ignore_errors=True)
        target.parent.mkdir(parents=True, exist_ok=True)
        clone_url = str(repository["clone_url"])
        self._validate_clone_url(repository["provider"], clone_url)
        work_ref = f"refs/heads/{manifest_item['work_branch']}"
        existing = self._run(
            ["git", "ls-remote", "--heads", "--", clone_url, work_ref],
            self.root,
            env,
        )
        branch = manifest_item["work_branch"] if existing.strip() else manifest_item["target_branch"]
        self._run(
            ["git", "clone", "--no-tags", "--single-branch", "--branch", branch, "--", clone_url, str(target)],
            self.root,
            env,
            timeout_seconds=900,
        )
        self._run(["git", "config", "core.hooksPath", "/dev/null"], target, env)
        self._run(["git", "config", "user.name", "画板 Developer Agent"], target, env)
        self._run(["git", "config", "user.email", "agent@huaban.local"], target, env)
        self._run(["git", "checkout", "-B", manifest_item["work_branch"]], target, env)
        if trusted_base is not None:
            self._restore_trusted_commit_base(
                target,
                developer_root,
                manifest_item,
                trusted_base,
                env,
            )
        _sync_untrusted_worktree(developer_root, target, changed_paths)
        return target

    def _restore_trusted_commit_base(
        self,
        target: Path,
        developer_root: Path,
        manifest_item: dict[str, Any],
        trusted_base: dict[str, Any],
        env: dict[str, str],
    ) -> None:
        """Rebuild an unpublished reviewed commit before applying the next rework diff."""
        expected_head = str(trusted_base.get("head_sha") or "").strip()
        expected_baseline = str(trusted_base.get("baseline_sha") or "").strip()
        expected_diff_sha = str(trusted_base.get("diff_sha256") or "").strip()
        if (
            not expected_head
            or expected_baseline != str(manifest_item.get("baseline_sha") or "")
            or len(expected_diff_sha) != 64
        ):
            raise GitProviderError(
                "git.commit_manifest_invalid",
                "prior reviewed commit metadata is incomplete or has a different baseline",
            )

        current_head = self._run(["git", "rev-parse", "HEAD"], target, env).strip()
        if current_head != expected_head:
            self._run(
                ["git", "cat-file", "-e", f"{expected_head}^{{commit}}"],
                developer_root,
                env,
            )
            source_diff = self._run(
                ["git", "diff", "--no-ext-diff", f"{expected_baseline}..{expected_head}"],
                developer_root,
                env,
                max_bytes=500_000,
            )
            if hashlib.sha256(source_diff.encode()).hexdigest() != expected_diff_sha:
                raise GitProviderError(
                    "git.diff_mismatch",
                    "prior reviewed commit no longer matches its trusted evidence",
                )
            self._run(
                ["git", "fetch", "--no-tags", "--", str(developer_root), expected_head],
                target,
                env,
            )
            self._run(
                ["git", "checkout", "-B", manifest_item["work_branch"], expected_head],
                target,
                env,
            )

        target_diff = self._run(
            ["git", "diff", "--no-ext-diff", f"{expected_baseline}..{expected_head}"],
            target,
            env,
            max_bytes=500_000,
        )
        if hashlib.sha256(target_diff.encode()).hexdigest() != expected_diff_sha:
            raise GitProviderError(
                "git.diff_mismatch",
                "restored reviewed commit does not match its trusted evidence",
            )

    def cleanup_stale(self, now: float | None = None) -> list[str]:
        cutoff = (now if now is not None else time.time()) - self.settings.workspace_ttl_hours * 3600
        removed: list[str] = []
        for path in self.root.iterdir():
            if not path.is_dir() or path.name.startswith(".") or not SAFE_ID.fullmatch(path.name):
                continue
            lease = self._lease_path(path.name)
            last_used = lease.stat().st_mtime if lease.exists() else path.stat().st_mtime
            if last_used < cutoff:
                shutil.rmtree(path)
                lease.unlink(missing_ok=True)
                removed.append(path.name)
        return removed

    def _lease_path(self, requirement_id: str) -> Path:
        return self.leases / _safe_id(requirement_id)

    def _touch_lease(self, requirement_id: str) -> None:
        self._lease_path(requirement_id).touch(exist_ok=True)

    def _validate_clone_url(self, provider: str, clone_url: str) -> None:
        validate_clone_url(
            provider,
            clone_url,
            self.settings.gitlab_base_url,
            allow_local_git=self.settings.allow_local_git,
        )

    def _git_env(self, provider: str) -> dict[str, str]:
        env = {
            "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
            "HOME": "/tmp/forgeflow-git-home",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_SSH_COMMAND": "ssh -o BatchMode=yes -o StrictHostKeyChecking=yes",
        }
        token = self.settings.github_token if provider == "github" else self.settings.gitlab_token
        if token:
            username = "x-access-token" if provider == "github" else "oauth2"
            credential = base64.b64encode(f"{username}:{token}".encode()).decode()
            env.update(
                {
                    "GIT_CONFIG_COUNT": "1",
                    "GIT_CONFIG_KEY_0": "http.extraHeader",
                    "GIT_CONFIG_VALUE_0": f"Authorization: Basic {credential}",
                }
            )
        return env

    @staticmethod
    def _run(
        argv: list[str],
        cwd: Path,
        env: dict[str, str],
        max_bytes: int = 200_000,
        timeout_seconds: int = 300,
    ) -> str:
        process: subprocess.Popen[bytes] | None = None
        try:
            process = subprocess.Popen(
                argv,
                cwd=cwd,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
            stdout, stderr = process.communicate(timeout=timeout_seconds)
        except subprocess.TimeoutExpired as exc:
            if process is not None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.communicate()
            raise GitProviderError("git.command_timeout", "Git command timed out", True) from exc
        output = stdout + stderr
        if process.returncode:
            safe = output[:4000].decode(errors="replace")
            raise GitProviderError("git.command_failed", f"Git command failed: {safe}")
        return output[:max_bytes].decode(errors="replace")

    @staticmethod
    def _scan_secrets(diff: str) -> None:
        added_lines = "\n".join(line[1:] for line in diff.splitlines() if line.startswith("+") and not line.startswith("+++"))
        if any(pattern.search(added_lines) for pattern in SECRET_PATTERNS):
            raise GitProviderError("git.secret_detected", "potential secret detected in generated changes")


def _safe_id(value: str) -> str:
    if not SAFE_ID.fullmatch(value):
        raise GitProviderError("git.invalid_identifier", "workspace identifier is invalid")
    return value


def _pull_request_body(context: dict[str, Any]) -> str:
    report = context.get("artifacts", {}).get("development_report", {})
    summary = report.get("summary", "Automated implementation produced by 画板.")
    return f"## 画板 delivery\n\n{summary}\n\nRequirement: `{context['requirement_id']}`\n"


async def _create_pull_request_or_manual_link(
    provider_factory: Callable[[str], GitProvider | None],
    repository: dict[str, Any],
    context: dict[str, Any],
) -> dict[str, Any]:
    provider = provider_factory(repository["provider"])
    if provider is None:
        full_name = "/".join(quote(part, safe="") for part in repository["full_name"].split("/"))
        base = quote(repository["target_branch"], safe="")
        head = quote(repository["work_branch"], safe="")
        if repository["provider"] == "github":
            url = f"https://github.com/{full_name}/compare/{base}...{head}?expand=1"
        else:
            url = f"https://gitlab.com/{full_name}/-/compare/{base}...{head}"
        return {"number": None, "url": url, "mode": "ssh_branch_only"}
    try:
        pull_request = await provider.create_or_update_pull_request(
            repository["full_name"],
            repository["work_branch"],
            repository["target_branch"],
            f"[画板] {context['title']}",
            _pull_request_body(context),
        )
        return {"number": pull_request.number, "url": pull_request.url, "mode": "pull_request"}
    finally:
        if hasattr(provider, "close"):
            await provider.close()  # type: ignore[attr-defined]


def _repository_changed_paths(
    context: dict[str, Any],
    manifest_item: dict[str, Any],
) -> set[Path] | None:
    raw_paths = context.get("artifacts", {}).get("development_report", {}).get("files_changed")
    if not isinstance(raw_paths, list) or not raw_paths:
        return None
    prefix = str(manifest_item.get("relative_path") or manifest_item.get("repository_id") or "").strip("/")
    selected: set[Path] = set()
    for value in raw_paths:
        if not isinstance(value, str):
            continue
        normalized = value.strip("/")
        if normalized == prefix:
            continue
        if prefix and not normalized.startswith(f"{prefix}/"):
            continue
        relative = normalized[len(prefix) + 1 :] if prefix else normalized
        path = Path(relative)
        if not relative or path.is_absolute() or ".." in path.parts or ".git" in path.parts:
            raise GitProviderError("git.unsafe_worktree_entry", "developer changed path is invalid")
        selected.add(path)
    return selected


def _repository_committed_state(
    context: dict[str, Any],
    manifest_item: dict[str, Any],
) -> dict[str, Any] | None:
    committed = context.get("artifacts", {}).get("development_commit_manifest")
    if not isinstance(committed, dict):
        return None
    repository_id = str(manifest_item.get("repository_id") or "")
    return next(
        (
            item
            for item in committed.get("repositories", [])
            if isinstance(item, dict) and str(item.get("repository_id") or "") == repository_id
        ),
        None,
    )


def _sync_untrusted_worktree(
    source: Path,
    destination: Path,
    changed_paths: set[Path] | None = None,
) -> None:
    if changed_paths is not None:
        ignored = _trusted_ignored_paths(destination, list(changed_paths))
        if ignored:
            raise GitProviderError(
                "git.unsafe_worktree_entry",
                "developer changed path targets an ignored dependency or build cache",
            )
        for relative in sorted(changed_paths):
            source_path = source / relative
            destination_path = destination / relative
            if not source_path.exists() and not source_path.is_symlink():
                if destination_path.is_dir() and not destination_path.is_symlink():
                    shutil.rmtree(destination_path)
                else:
                    destination_path.unlink(missing_ok=True)
                continue
            if source_path.is_symlink() or not source_path.is_file():
                raise GitProviderError(
                    "git.unsafe_worktree_entry",
                    f"developer change contains a symlink or special file: {relative.as_posix()}",
                )
            destination_path.parent.mkdir(parents=True, exist_ok=True)
            if destination_path.is_symlink() or destination_path.is_dir():
                if destination_path.is_dir() and not destination_path.is_symlink():
                    shutil.rmtree(destination_path)
                else:
                    destination_path.unlink()
            shutil.copy2(source_path, destination_path)
        return

    tracked_result = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=destination,
        capture_output=True,
        check=False,
    )
    if tracked_result.returncode:
        raise GitProviderError("git.command_failed", "Unable to enumerate trusted repository files")
    tracked = {
        Path(value.decode(errors="surrogateescape"))
        for value in tracked_result.stdout.split(b"\0")
        if value
    }

    # Dependency preparation intentionally leaves ignored caches (for example
    # node_modules) in the developer workspace.  Do not copy or security-scan
    # those caches as source changes.  Ignore decisions are made by the clean,
    # trusted checkout before any developer-controlled files are overlaid.
    source_entries: dict[Path, Path] = {}
    for current, directory_names, file_names in os.walk(source, topdown=True, followlinks=False):
        current_path = Path(current)
        relative_root = current_path.relative_to(source)
        candidates: list[Path] = []
        directory_paths: dict[str, tuple[Path, Path]] = {}
        for name in directory_names:
            if relative_root == Path(".") and name == ".git":
                continue
            path = current_path / name
            relative = path.relative_to(source)
            directory_paths[name] = (relative, path)
            candidates.append(relative)
        file_paths: dict[str, tuple[Path, Path]] = {}
        for name in file_names:
            if relative_root == Path(".") and name == ".git":
                continue
            path = current_path / name
            relative = path.relative_to(source)
            file_paths[name] = (relative, path)
            candidates.append(relative)

        ignored = _trusted_ignored_paths(destination, candidates)
        retained_directories: list[str] = []
        for name, (relative, path) in directory_paths.items():
            if relative in ignored:
                continue
            if path.is_symlink():
                source_entries[relative] = path
                continue
            retained_directories.append(name)
        directory_names[:] = retained_directories
        for relative, path in file_paths.values():
            if relative not in ignored:
                source_entries[relative] = path

    for relative in tracked:
        source_path = source / relative
        destination_path = destination / relative
        if not source_path.exists() and not source_path.is_symlink():
            if destination_path.is_dir() and not destination_path.is_symlink():
                shutil.rmtree(destination_path)
            else:
                destination_path.unlink(missing_ok=True)
            continue
        if source_path.is_symlink():
            if not destination_path.is_symlink() or os.readlink(source_path) != os.readlink(destination_path):
                raise GitProviderError(
                    "git.unsafe_worktree_entry",
                    f"developer change contains an unsupported symlink: {relative.as_posix()}",
                )
            source_entries.pop(relative, None)
            continue
        if not source_path.is_file():
            raise GitProviderError(
                "git.unsafe_worktree_entry",
                f"developer change contains a special file: {relative.as_posix()}",
            )
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        if destination_path.is_symlink() or destination_path.is_dir():
            if destination_path.is_dir() and not destination_path.is_symlink():
                shutil.rmtree(destination_path)
            else:
                destination_path.unlink()
        shutil.copy2(source_path, destination_path)
        source_entries.pop(relative, None)

    for relative, source_path in source_entries.items():
        if source_path.is_symlink() or not source_path.is_file():
            raise GitProviderError(
                "git.unsafe_worktree_entry",
                f"developer change contains a symlink or special file: {relative.as_posix()}",
            )
        destination_path = destination / relative
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_path, destination_path)


def _trusted_ignored_paths(repository: Path, paths: list[Path]) -> set[Path]:
    if not paths:
        return set()
    payload = b"\0".join(os.fsencode(path.as_posix()) for path in paths) + b"\0"
    completed = subprocess.run(
        ["git", "check-ignore", "-z", "--stdin"],
        cwd=repository,
        input=payload,
        capture_output=True,
        check=False,
    )
    if completed.returncode not in {0, 1}:
        raise GitProviderError("git.command_failed", "Unable to evaluate trusted ignore rules")
    return {
        Path(os.fsdecode(value))
        for value in completed.stdout.split(b"\0")
        if value
    }


def _analysis_path_hints(context: dict[str, Any]) -> set[str]:
    requirement_text = f"{context.get('title', '')}\n{context.get('description', '')}"
    candidates = re.findall(
        r"(?:[A-Za-z0-9_.@+-]+/)*[A-Za-z0-9_.@+-]+\.[A-Za-z0-9]{1,10}",
        requirement_text,
    )
    return {candidate.lower() for candidate in candidates if len(candidate) <= 240}


def _repository_analysis_snapshot(
    root: Path,
    content_budget: int,
    path_hints: set[str] | None = None,
) -> tuple[dict[str, Any], int]:
    tree: list[str] = []
    selected: list[dict[str, str]] = []
    extension_counts: dict[str, int] = {}
    used = 0
    targeted_candidates: list[Path] = []
    baseline_candidates: list[Path] = []
    path_hints = path_hints or set()
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if ".git" in relative.parts or path.is_symlink() or not path.is_file():
            continue
        relative_name = relative.as_posix()
        if len(tree) < 800:
            tree.append(relative_name)
        suffix = path.suffix.lower() or "[none]"
        extension_counts[suffix] = extension_counts.get(suffix, 0) + 1
        lowered = path.name.lower()
        lowered_relative = relative_name.lower()
        is_requested_path = any(
            lowered_relative == hint
            or lowered_relative.endswith(f"/{hint}")
            or hint in lowered_relative
            for hint in path_hints
        )
        is_ci_configuration = (
            len(relative.parts) >= 3
            and relative.parts[0].lower() == ".github"
            and relative.parts[1].lower() == "workflows"
            and path.suffix.lower() in {".yml", ".yaml"}
        ) or (
            len(relative.parts) >= 2
            and relative.parts[0].lower() == ".circleci"
            and lowered in {"config.yml", "config.yaml"}
        )
        is_baseline_context = (
            lowered.startswith("readme")
            or lowered in ANALYSIS_FILENAMES
            or (lowered.startswith("tsconfig") and path.suffix.lower() == ".json")
            or (relative.parts and relative.parts[0].lower() in {"docs", "architecture"} and suffix == ".md")
            or is_ci_configuration
        )
        if is_requested_path:
            targeted_candidates.append(path)
        elif is_baseline_context:
            baseline_candidates.append(path)
    candidates = list(dict.fromkeys([*targeted_candidates, *baseline_candidates]))
    targeted = set(targeted_candidates)
    for path in candidates[:30]:
        if used >= content_budget:
            break
        if path.stat().st_size > 64_000:
            continue
        data = path.read_bytes()
        if b"\x00" in data:
            continue
        remaining = min(12_000, content_budget - used)
        content = data[:remaining].decode("utf-8", errors="replace")
        used += len(content.encode("utf-8"))
        selected.append(
            {
                "path": path.relative_to(root).as_posix(),
                "content": content,
                "selection_reason": "requirement_reference" if path in targeted else "repository_baseline",
            }
        )
    return {
        "file_tree": tree,
        "file_tree_truncated": len(tree) >= 800,
        "file_type_counts": dict(sorted(extension_counts.items(), key=lambda item: (-item[1], item[0]))[:30]),
        "selected_files": selected,
    }, used


def _directory_size(root: Path, excluded: set[Path] | None = None) -> int:
    excluded = {path.resolve() for path in (excluded or set())}
    total = 0
    for directory, names, files in os.walk(root):
        current = Path(directory).resolve()
        names[:] = [name for name in names if (current / name).resolve() not in excluded]
        for name in files:
            try:
                total += (current / name).stat().st_size
            except FileNotFoundError:
                continue
    return total
