from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Callable

from ..core.config import Settings
from .diagnostics import safe_error_message


RunCommand = Callable[[list[str], Path, dict[str, str], int], subprocess.CompletedProcess[str]]


class DependencyPreparationError(RuntimeError):
    def __init__(self, code: str, message: str, retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class DependencyPreparer:
    """Install locked Node dependencies in the network-enabled dependency worker."""

    def __init__(self, settings: Settings, runner: RunCommand | None = None):
        self.settings = settings
        self.workspace_root = settings.workspace_root.resolve()
        self.cache_root = settings.dependency_cache_root.resolve()
        self.cache_root.mkdir(parents=True, exist_ok=True)
        self.runner = runner or _run_command

    def prepare(
        self,
        context: dict[str, Any],
        manifest_kind: str = "workspace_manifest",
        scope: str = "development",
    ) -> dict[str, Any]:
        manifest = context.get("artifacts", {}).get(manifest_kind)
        if not manifest or not manifest.get("workspace_root"):
            raise DependencyPreparationError(
                "dependency.workspace_missing",
                "workspace manifest is missing",
            )
        root = Path(str(manifest["workspace_root"])).resolve()
        if not root.is_relative_to(self.workspace_root):
            raise DependencyPreparationError(
                "dependency.workspace_escape",
                "workspace is outside configured root",
            )

        repositories: list[dict[str, Any]] = []
        for item in manifest.get("repositories", []):
            relative_path = str(item.get("relative_path") or item.get("repository_id") or "")
            repository_root = (root / relative_path).resolve()
            if not repository_root.is_relative_to(root) or not repository_root.is_dir():
                raise DependencyPreparationError(
                    "dependency.repository_missing",
                    "repository workspace is missing",
                )
            repositories.append(self._prepare_repository(repository_root, item))

        if _directory_size(root) > self.settings.workspace_max_bytes:
            raise DependencyPreparationError(
                "dependency.workspace_quota_exceeded",
                "workspace storage quota exceeded after dependency installation",
            )
        installed = sum(item["status"] == "installed" for item in repositories)
        return {
            "schema_version": "1.0",
            "scope": scope,
            "network_execution": True,
            "install_scripts": self.settings.dependency_install_scripts,
            "summary": f"依赖准备完成：安装 {installed} 个仓库，跳过 {len(repositories) - installed} 个仓库。",
            "repositories": repositories,
        }

    def _prepare_repository(
        self,
        repository_root: Path,
        item: dict[str, Any],
    ) -> dict[str, Any]:
        command = _dependency_command(repository_root, self.settings.dependency_install_scripts)
        repository_id = str(item.get("repository_id", ""))
        if command is None:
            return {
                "repository_id": repository_id,
                "status": "skipped",
                "reason": "未发现受支持的 Node 锁文件",
            }

        manager, lockfile, argv = command
        environment = _dependency_environment(
            self.cache_root,
            self.settings.dependency_install_scripts,
        )
        started = time.monotonic()
        try:
            completed = self.runner(
                argv,
                repository_root,
                environment,
                self.settings.dependency_install_timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            raise DependencyPreparationError(
                "dependency.timeout",
                f"{manager} dependency installation timed out after {exc.timeout} seconds",
                True,
            ) from exc
        except OSError as exc:
            raise DependencyPreparationError(
                "dependency.executor_unavailable",
                safe_error_message(exc),
                True,
            ) from exc

        duration = round(time.monotonic() - started, 3)
        if completed.returncode != 0:
            detail = safe_error_message(
                (completed.stdout or "") + "\n" + (completed.stderr or ""),
                3_000,
            )
            retryable = _looks_like_network_failure(detail)
            raise DependencyPreparationError(
                "dependency.network" if retryable else "dependency.install_failed",
                f"{manager} dependency installation failed: {detail or f'exit code {completed.returncode}'}",
                retryable,
            )

        prepared_tools = self._prepare_build_tool_caches(
            repository_root,
            environment,
        )

        return {
            "repository_id": repository_id,
            "status": "installed",
            "manager": manager,
            "lockfile": lockfile,
            "duration_seconds": duration,
            "prepared_tools": prepared_tools,
        }

    def _prepare_build_tool_caches(
        self,
        repository_root: Path,
        environment: dict[str, str],
    ) -> list[dict[str, str]]:
        """Download declared packager runtimes without executing repository scripts."""
        pkg_fetch = repository_root / "node_modules" / ".bin" / "pkg-fetch"
        if not pkg_fetch.exists():
            return []
        node_ranges = _declared_pkg_node_ranges(repository_root)
        prepared: list[dict[str, str]] = []
        for node_range in node_ranges:
            argv = [str(pkg_fetch), "--node-range", node_range]
            shared_pkg_cache = self.cache_root / "pkg"
            pkg_environment = {
                **environment,
                "PKG_CACHE_PATH": str(shared_pkg_cache),
            }
            try:
                completed = self.runner(
                    argv,
                    repository_root,
                    pkg_environment,
                    self.settings.dependency_install_timeout_seconds,
                )
            except subprocess.TimeoutExpired as exc:
                raise DependencyPreparationError(
                    "dependency.timeout",
                    f"pkg runtime preparation timed out after {exc.timeout} seconds",
                    True,
                ) from exc
            except OSError as exc:
                raise DependencyPreparationError(
                    "dependency.executor_unavailable",
                    safe_error_message(exc),
                    True,
                ) from exc
            if completed.returncode != 0:
                detail = safe_error_message(
                    (completed.stdout or "") + "\n" + (completed.stderr or ""),
                    3_000,
                )
                retryable = _looks_like_network_failure(detail)
                raise DependencyPreparationError(
                    "dependency.network" if retryable else "dependency.tool_cache_failed",
                    f"pkg runtime preparation failed: {detail or f'exit code {completed.returncode}'}",
                    retryable,
                )
            workspace_pkg_cache = repository_root / "node_modules" / ".cache" / "pkg"
            workspace_pkg_cache.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(shared_pkg_cache, workspace_pkg_cache, dirs_exist_ok=True)
            prepared.append(
                {
                    "tool": "@yao-pkg/pkg-fetch",
                    "target": node_range,
                    "status": "prepared",
                }
            )
        return prepared


def _dependency_command(
    repository_root: Path,
    install_scripts: bool,
) -> tuple[str, str, list[str]] | None:
    package_json = repository_root / "package.json"
    lockfiles = [
        name
        for name in ("npm-shrinkwrap.json", "package-lock.json", "pnpm-lock.yaml", "yarn.lock")
        if (repository_root / name).is_file()
    ]
    if not package_json.is_file() and not lockfiles:
        return None
    if not package_json.is_file():
        raise DependencyPreparationError(
            "dependency.manifest_missing",
            "Node lockfile exists but package.json is missing",
        )
    if not lockfiles:
        return None

    npm_locks = [name for name in lockfiles if name in {"npm-shrinkwrap.json", "package-lock.json"}]
    managers = set()
    if npm_locks:
        managers.add("npm")
    if "pnpm-lock.yaml" in lockfiles:
        managers.add("pnpm")
    if "yarn.lock" in lockfiles:
        managers.add("yarn")
    if len(managers) != 1:
        raise DependencyPreparationError(
            "dependency.lockfile_ambiguous",
            "multiple package-manager lockfiles are present",
        )

    manager = managers.pop()
    if manager == "npm":
        lockfile = "npm-shrinkwrap.json" if "npm-shrinkwrap.json" in npm_locks else "package-lock.json"
        argv = ["npm", "ci", "--no-audit", "--no-fund"]
        if not install_scripts:
            argv.append("--ignore-scripts")
        return manager, lockfile, argv
    if manager == "pnpm":
        argv = ["corepack", "pnpm", "install", "--frozen-lockfile"]
        if not install_scripts:
            argv.append("--ignore-scripts")
        return manager, "pnpm-lock.yaml", argv

    package_manager = ""
    try:
        package_manager = str(json.loads(package_json.read_text()).get("packageManager", ""))
    except (OSError, json.JSONDecodeError) as exc:
        raise DependencyPreparationError(
            "dependency.manifest_invalid",
            "package.json is not valid JSON",
        ) from exc
    major_match = re.fullmatch(r"yarn@(\d+)(?:\..*)?", package_manager)
    modern_yarn = bool(major_match and int(major_match.group(1)) >= 2)
    argv = ["corepack", "yarn", "install", "--immutable" if modern_yarn else "--frozen-lockfile"]
    if not install_scripts and not modern_yarn:
        argv.append("--ignore-scripts")
    return manager, "yarn.lock", argv


def _dependency_environment(cache_root: Path, install_scripts: bool) -> dict[str, str]:
    environment = {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "HOME": "/tmp/forgeflow-dependency-home",
        "CI": "1",
        "NO_COLOR": "1",
        "NPM_CONFIG_CACHE": str(cache_root / "npm"),
        "COREPACK_HOME": str(cache_root / "corepack"),
        "COREPACK_ENABLE_DOWNLOAD_PROMPT": "0",
    }
    if not install_scripts:
        environment["YARN_ENABLE_SCRIPTS"] = "false"
    return environment


def _declared_pkg_node_ranges(repository_root: Path) -> list[str]:
    """Infer explicit pkg Node targets from bounded, referenced build files."""
    package_json = repository_root / "package.json"
    try:
        raw_manifest = package_json.read_text()
        manifest = json.loads(raw_manifest)
    except (OSError, json.JSONDecodeError):
        return []
    dependency_names = {
        *manifest.get("dependencies", {}).keys(),
        *manifest.get("devDependencies", {}).keys(),
    }
    if "@yao-pkg/pkg" not in dependency_names and "pkg" not in dependency_names:
        return []

    sources = [raw_manifest]
    for command in manifest.get("scripts", {}).values():
        if not isinstance(command, str):
            continue
        for match in re.finditer(r"(?:^|\s)node\s+([^\s;&|]+\.c?js)(?:\s|$)", command):
            candidate = (repository_root / match.group(1)).resolve()
            if (
                candidate.is_relative_to(repository_root)
                and candidate.is_file()
                and candidate.stat().st_size <= 128_000
            ):
                try:
                    sources.append(candidate.read_text())
                except OSError:
                    continue
    majors = {
        match.group(1)
        for source in sources
        for match in re.finditer(r"\bnode(\d{1,2})(?=[-${\s'\"])", source)
    }
    return [f"node{major}" for major in sorted(majors, key=int)]


def _run_command(
    argv: list[str],
    cwd: Path,
    environment: dict[str, str],
    timeout: int,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        cwd=cwd,
        env=environment,
        capture_output=True,
        text=True,
        errors="replace",
        timeout=min(max(timeout, 1), 3_600),
        check=False,
    )


def _looks_like_network_failure(message: str) -> bool:
    lowered = message.lower()
    if any(
        marker in lowered
        for marker in (
            "enospc",
            "no space left on device",
            "enomem",
            "out of memory",
            "permission denied",
        )
    ):
        return False
    return any(
        marker in lowered
        for marker in (
            "eai_again",
            "enotfound",
            "econnreset",
            "etimedout",
            "network timeout",
            "socket timeout",
            "http 429",
            "http 500",
            "http 502",
            "http 503",
            "http 504",
        )
    )


def _directory_size(root: Path) -> int:
    total = 0
    for path in root.rglob("*"):
        try:
            if path.is_file() and not path.is_symlink():
                total += path.stat().st_size
        except FileNotFoundError:
            continue
    return total


__all__ = ["DependencyPreparationError", "DependencyPreparer"]
