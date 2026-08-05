from __future__ import annotations

import base64
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from ..core.config import Settings
from ..providers.git import GitProvider, GitProviderError


SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
SECRET_PATTERNS = [
    re.compile(r"(?i)(api[_-]?key|secret|password|token)\s*[:=]\s*['\"]?[A-Za-z0-9_./+=-]{12,}"),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
]
ANALYSIS_FILENAMES = {
    "package.json",
    "pyproject.toml",
    "requirements.txt",
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
            )
            work_branch = f"forgeflow/req-{requirement_id[:12]}"
            self._run(["git", "config", "core.hooksPath", "/dev/null"], target, env)
            self._run(["git", "config", "user.name", "ForgeFlow Developer Agent"], target, env)
            self._run(["git", "config", "user.email", "agent@forgeflow.local"], target, env)
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

    def prepare_analysis(self, context: dict[str, Any]) -> dict[str, Any]:
        requirement_id = _safe_id(context["requirement_id"])
        workspace_id = _safe_id(f"{requirement_id}-analysis")
        self.cleanup_stale()
        analysis_root = self.root / workspace_id
        shutil.rmtree(analysis_root, ignore_errors=True)
        analysis_root.mkdir(parents=True)
        snapshots: list[dict[str, Any]] = []
        remaining_content_bytes = 100_000
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
                )
                self._run(["git", "config", "core.hooksPath", "/dev/null"], target, env)
                head_sha = self._run(["git", "rev-parse", "HEAD"], target, env).strip()
                snapshot, used = _repository_analysis_snapshot(target, remaining_content_bytes)
                remaining_content_bytes -= used
                snapshots.append(
                    {
                        "requirement_repository_id": repository["requirement_repository_id"],
                        "repository_id": repository_id,
                        "provider": repository["provider"],
                        "full_name": repository["full_name"],
                        "target_branch": repository["target_branch"],
                        "head_sha": head_sha,
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
                    ["git", "clone", "--no-tags", "--single-branch", "--branch", branch, "--", clone_url, str(target)],
                    self.root,
                    env,
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
        provider_factory: Callable[[str], GitProvider],
    ) -> dict[str, Any]:
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
                provider = provider_factory(item["provider"])
                try:
                    pull_request = await provider.create_or_update_pull_request(
                        item["full_name"],
                        item["work_branch"],
                        item["target_branch"],
                        f"[ForgeFlow] {context['title']}",
                        _pull_request_body(context),
                    )
                finally:
                    if hasattr(provider, "close"):
                        await provider.close()  # type: ignore[attr-defined]
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
                    "pull_request_number": pull_request.number,
                    "pull_request_url": pull_request.url,
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

    def _trusted_publish_checkout(
        self,
        requirement_id: str,
        repository: dict[str, Any],
        manifest_item: dict[str, Any],
        developer_root: Path,
        env: dict[str, str],
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
        )
        self._run(["git", "config", "core.hooksPath", "/dev/null"], target, env)
        self._run(["git", "config", "user.name", "ForgeFlow Developer Agent"], target, env)
        self._run(["git", "config", "user.email", "agent@forgeflow.local"], target, env)
        self._run(["git", "checkout", "-B", manifest_item["work_branch"]], target, env)
        _sync_untrusted_worktree(developer_root, target)
        return target

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
        parsed = urlparse(clone_url)
        if parsed.scheme == "file" and self.settings.allow_local_git:
            return
        if parsed.scheme != "https" or parsed.username or parsed.password:
            raise GitProviderError("git.invalid_clone_url", "clone URL must be credential-free HTTPS")
        hostname = (parsed.hostname or "").lower()
        expected = "github.com" if provider == "github" else (urlparse(self.settings.gitlab_base_url).hostname or "").lower()
        if hostname != expected:
            raise GitProviderError("git.clone_host_denied", "clone host does not match provider configuration")

    def _git_env(self, provider: str) -> dict[str, str]:
        env = {
            "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
            "HOME": "/tmp/forgeflow-git-home",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_CONFIG_NOSYSTEM": "1",
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
    def _run(argv: list[str], cwd: Path, env: dict[str, str], max_bytes: int = 200_000) -> str:
        try:
            completed = subprocess.run(argv, cwd=cwd, env=env, capture_output=True, timeout=300, check=False)
        except subprocess.TimeoutExpired as exc:
            raise GitProviderError("git.command_timeout", "Git command timed out", True) from exc
        output = completed.stdout + completed.stderr
        if completed.returncode:
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
    summary = report.get("summary", "Automated implementation produced by ForgeFlow.")
    return f"## ForgeFlow delivery\n\n{summary}\n\nRequirement: `{context['requirement_id']}`\n"


def _sync_untrusted_worktree(source: Path, destination: Path) -> None:
    for path in source.rglob("*"):
        relative = path.relative_to(source)
        if ".git" in relative.parts:
            continue
        if path.is_symlink() or (not path.is_file() and not path.is_dir()):
            raise GitProviderError(
                "git.unsafe_worktree_entry",
                "developer worktree contains a symlink or special file",
            )
    for child in destination.iterdir():
        if child.name == ".git":
            continue
        if child.is_dir():
            shutil.rmtree(child)
        else:
            child.unlink()
    for child in source.iterdir():
        if child.name == ".git":
            continue
        target = destination / child.name
        if child.is_dir():
            shutil.copytree(child, target)
        else:
            shutil.copy2(child, target)


def _repository_analysis_snapshot(root: Path, content_budget: int) -> tuple[dict[str, Any], int]:
    tree: list[str] = []
    selected: list[dict[str, str]] = []
    extension_counts: dict[str, int] = {}
    used = 0
    candidates: list[Path] = []
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
        if (
            lowered.startswith("readme")
            or lowered in ANALYSIS_FILENAMES
            or (relative.parts and relative.parts[0].lower() in {"docs", "architecture"} and suffix == ".md")
        ):
            candidates.append(path)
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
        selected.append({"path": path.relative_to(root).as_posix(), "content": content})
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
