import subprocess
from pathlib import Path

import pytest

from app.core.config import Settings
from app.dependency_worker import execute_task
from app.services.dependencies import DependencyPreparationError, DependencyPreparer


def _workspace(tmp_path: Path, files: dict[str, str]) -> tuple[Settings, dict]:
    workspace_root = tmp_path / "workspaces"
    repository = workspace_root / "req-1" / "repo-1"
    repository.mkdir(parents=True)
    for name, content in files.items():
        path = repository / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    settings = Settings(
        _env_file=None,
        workspace_root=workspace_root,
        dependency_cache_root=tmp_path / "dependency-cache",
    )
    context = {
        "artifacts": {
            "workspace_manifest": {
                "workspace_root": str(repository.parent),
                "repositories": [
                    {"repository_id": "repo-1", "relative_path": "repo-1"}
                ],
            }
        }
    }
    return settings, context


def test_npm_dependencies_are_installed_from_lockfile_without_scripts(tmp_path: Path) -> None:
    settings, context = _workspace(
        tmp_path,
        {"package.json": '{"name":"example"}', "package-lock.json": "{}"},
    )
    calls = []

    def runner(argv, cwd, environment, timeout):
        calls.append((argv, cwd, environment, timeout))
        (cwd / "node_modules").mkdir()
        return subprocess.CompletedProcess(argv, 0, "installed", "")

    result = DependencyPreparer(settings, runner).prepare(context)

    assert calls[0][0] == [
        "npm",
        "ci",
        "--no-audit",
        "--no-fund",
        "--ignore-scripts",
    ]
    assert calls[0][1].name == "repo-1"
    assert calls[0][2]["NPM_CONFIG_CACHE"].endswith("dependency-cache/npm")
    assert result["network_execution"] is True
    assert result["repositories"][0]["status"] == "installed"
    assert result["repositories"][0]["lockfile"] == "package-lock.json"


def test_repository_without_supported_lockfile_is_skipped(tmp_path: Path) -> None:
    settings, context = _workspace(tmp_path, {"README.md": "# Example\n"})

    result = DependencyPreparer(settings).prepare(context)

    assert result["repositories"] == [
        {
            "repository_id": "repo-1",
            "status": "skipped",
            "reason": "未发现受支持的 Node 锁文件",
        }
    ]


def test_pkg_runtime_is_prefetched_from_declared_target_without_running_repository_script(
    tmp_path: Path,
) -> None:
    settings, context = _workspace(
        tmp_path,
        {
            "package.json": (
                '{"name":"example","devDependencies":{"@yao-pkg/pkg":"1.0.0"},'
                '"scripts":{"package:cli":"node scripts/package-cli.js"}}'
            ),
            "package-lock.json": "{}",
            "scripts/package-cli.js": "const target = `node20-${platform}-${arch}`\n",
        },
    )
    repository = Path(context["artifacts"]["workspace_manifest"]["workspace_root"]) / "repo-1"
    calls = []

    def runner(argv, cwd, environment, timeout):
        calls.append((argv, cwd, environment, timeout))
        if argv[0] == "npm":
            binary = cwd / "node_modules" / ".bin" / "pkg-fetch"
            binary.parent.mkdir(parents=True)
            binary.write_text("#!/usr/bin/env node\n")
        else:
            shared_cache = Path(environment["PKG_CACHE_PATH"]) / "v3.5"
            shared_cache.mkdir(parents=True)
            (shared_cache / "fetched-v20-linux-arm64").write_text("runtime")
        return subprocess.CompletedProcess(argv, 0, "prepared", "")

    result = DependencyPreparer(settings, runner).prepare(context)

    assert calls[1][0][-2:] == ["--node-range", "node20"]
    assert calls[1][2]["PKG_CACHE_PATH"].endswith("dependency-cache/pkg")
    assert (repository / "node_modules" / ".cache" / "pkg").is_dir()
    assert result["repositories"][0]["prepared_tools"] == [
        {"tool": "@yao-pkg/pkg-fetch", "target": "node20", "status": "prepared"}
    ]


def test_network_install_failure_is_retryable_and_redacted(tmp_path: Path) -> None:
    settings, context = _workspace(
        tmp_path,
        {"package.json": '{"name":"example"}', "package-lock.json": "{}"},
    )

    def runner(argv, cwd, environment, timeout):
        return subprocess.CompletedProcess(
            argv,
            1,
            "",
            "npm ERR EAI_AGAIN https://user:password@registry.npmjs.org/package",
        )

    with pytest.raises(DependencyPreparationError) as raised:
        DependencyPreparer(settings, runner).prepare(context)

    assert raised.value.code == "dependency.network"
    assert raised.value.retryable is True
    assert "user:password" not in str(raised.value)


def test_resource_exhaustion_takes_precedence_over_network_wording(tmp_path: Path) -> None:
    settings, context = _workspace(
        tmp_path,
        {"package.json": '{"name":"example"}', "package-lock.json": "{}"},
    )

    def runner(argv, cwd, environment, timeout):
        return subprocess.CompletedProcess(
            argv,
            1,
            "",
            "Network error during fallback build: ENOSPC: no space left on device",
        )

    with pytest.raises(DependencyPreparationError) as raised:
        DependencyPreparer(settings, runner).prepare(context)

    assert raised.value.code == "dependency.install_failed"
    assert raised.value.retryable is False


def test_multiple_package_manager_lockfiles_are_rejected(tmp_path: Path) -> None:
    settings, context = _workspace(
        tmp_path,
        {
            "package.json": '{"name":"example"}',
            "package-lock.json": "{}",
            "pnpm-lock.yaml": "lockfileVersion: 9",
        },
    )

    with pytest.raises(DependencyPreparationError) as raised:
        DependencyPreparer(settings).prepare(context)

    assert raised.value.code == "dependency.lockfile_ambiguous"


@pytest.mark.asyncio
async def test_dependency_worker_executes_prepare_task(tmp_path: Path) -> None:
    settings, context = _workspace(tmp_path, {"README.md": "# Example\n"})
    result = await execute_task(
        {
            "task_id": "task-1",
            "task_type": "dependency.prepare",
            "payload": {"context": context},
        },
        DependencyPreparer(settings),
    )

    assert result["status"] == "completed"
    assert result["output"]["scope"] == "development"


@pytest.mark.asyncio
async def test_dependency_worker_prepares_clean_acceptance_workspace(tmp_path: Path) -> None:
    settings, context = _workspace(tmp_path, {"README.md": "# Example\n"})
    context["artifacts"]["verification_manifest"] = context["artifacts"].pop(
        "workspace_manifest"
    )
    result = await execute_task(
        {
            "task_id": "task-verify",
            "task_type": "dependency.prepare_verification",
            "payload": {"context": context},
        },
        DependencyPreparer(settings),
    )

    assert result["status"] == "completed"
    assert result["output"]["scope"] == "acceptance"
