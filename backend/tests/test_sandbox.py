from pathlib import Path
import resource
import subprocess
import threading

import pytest

from app.agents.sandbox import (
    SandboxViolation,
    WorkspaceSandbox,
    _command_file_size_limit,
    _limit_process,
)
from app.core.config import Settings
from app.sandbox_executor import SandboxRequestHandler, ThreadingUnixServer


def test_workspace_sandbox_reads_writes_and_runs_allowlisted_command(tmp_path: Path) -> None:
    sandbox = WorkspaceSandbox(tmp_path)
    sandbox.write_file("src/value.txt", "safe")
    assert sandbox.read_file("src/value.txt") == "safe"
    assert sandbox.list_files() == ["src/value.txt"]
    result = sandbox.run(["python", "-c", "print('ok')"])
    assert result.returncode == 0
    assert result.output.strip() == "ok"


def test_workspace_sandbox_replace_text_is_idempotent_when_new_text_already_exists(tmp_path: Path) -> None:
    target = tmp_path / "value.txt"
    target.write_text("before\nupdated\nafter\n")
    sandbox = WorkspaceSandbox(tmp_path)

    sandbox.replace_text("value.txt", "old value", "updated")

    assert target.read_text() == "before\nupdated\nafter\n"


def test_workspace_sandbox_searches_literal_text_with_bounded_exclusions(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "service.py").write_text("def target_service():\n    return 'ok'\n")
    (tmp_path / "node_modules" / "dependency").mkdir(parents=True)
    (tmp_path / "node_modules" / "dependency" / "index.py").write_text("def target_service(): pass\n")
    sandbox = WorkspaceSandbox(tmp_path)

    matches = sandbox.search_text("target_service", "src")

    assert matches == [
        {"path": "src/service.py", "line": 1, "text": "def target_service():"}
    ]
    assert sandbox.search_text("target_service") == matches


def test_workspace_sandbox_delete_file_is_idempotent(tmp_path: Path) -> None:
    target = tmp_path / "obsolete.txt"
    target.write_text("obsolete\n")
    sandbox = WorkspaceSandbox(tmp_path)

    assert sandbox.delete_file("obsolete.txt") is True
    assert sandbox.delete_file("obsolete.txt") is False


def test_workspace_sandbox_blocks_escape_and_restricted_package_commands(tmp_path: Path) -> None:
    sandbox = WorkspaceSandbox(tmp_path)
    with pytest.raises(SandboxViolation, match="escapes"):
        sandbox.read_file("../secret")
    with pytest.raises(SandboxViolation, match="not allowed"):
        sandbox.run(["pnpm", "prune"])
    with pytest.raises(SandboxViolation, match="npx tool is not allowlisted"):
        sandbox.run(["npx", "create-react-app", "demo"])


@pytest.mark.parametrize("argv", [["npm", "ci"], ["npm", "install"], ["npm", "run", "build"], ["npm", "exec", "jest"], ["grep", "-R", "needle", "."], ["rg", "needle"]])
def test_workspace_sandbox_allows_npm_and_search_commands(
    monkeypatch,
    tmp_path: Path,
    argv: list[str],
) -> None:
    observed: dict[str, list[str]] = {}

    def fake_run(command, **_kwargs):
        observed["argv"] = command

        class Result:
            stdout = "ok\n"
            stderr = ""
            returncode = 0

        return Result()

    monkeypatch.setattr("app.agents.sandbox.subprocess.run", fake_run)
    sandbox = WorkspaceSandbox(tmp_path, discover_executor=False)

    result = sandbox.run(argv)

    assert observed["argv"] == argv
    assert result.returncode == 0


def test_workspace_sandbox_blocks_git_metadata_for_every_file_operation(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    (workspace / ".git").mkdir(parents=True)
    (workspace / ".git" / "config").write_text("credential = secret")
    (workspace / "safe.txt").write_text("safe")
    (workspace / "git-link").symlink_to(workspace / ".git" / "config")
    sandbox = WorkspaceSandbox(workspace, discover_executor=False)

    with pytest.raises(SandboxViolation, match="Git metadata"):
        sandbox.read_file(".git/config")
    with pytest.raises(SandboxViolation, match="Git metadata"):
        sandbox.read_file("git-link")
    with pytest.raises(SandboxViolation, match="absolute paths"):
        sandbox.read_file(str(workspace / "safe.txt"))


def test_workspace_sandbox_forces_local_only_npx_validation(monkeypatch, tmp_path: Path) -> None:
    sandbox = WorkspaceSandbox(tmp_path)
    observed: dict[str, list[str]] = {}
    local_bin = tmp_path / "node_modules" / ".bin"
    local_bin.mkdir(parents=True)
    (local_bin / "tsc").write_text("")

    def fake_run(argv, **_kwargs):
        observed["argv"] = argv

        class Result:
            stdout = "ok"
            stderr = ""
            returncode = 0

        return Result()

    monkeypatch.setattr("app.agents.sandbox.subprocess.run", fake_run)

    result = sandbox.run(["npx", "tsc", "--noEmit"])

    assert observed["argv"] == ["npx", "--no-install", "tsc", "--noEmit"]
    assert result.returncode == 0


def test_workspace_sandbox_blocks_package_manager_exec(tmp_path: Path) -> None:
    sandbox = WorkspaceSandbox(tmp_path, discover_executor=False)

    with pytest.raises(SandboxViolation, match="package-manager exec commands are not allowed"):
        sandbox.run(["pnpm", "exec", "jest", "tests/unit/value.test.ts"])


def test_workspace_sandbox_blocks_missing_local_npx_tool(tmp_path: Path) -> None:
    sandbox = WorkspaceSandbox(tmp_path, discover_executor=False)

    with pytest.raises(SandboxViolation, match="npx tool is not installed in the workspace"):
        sandbox.run(["npx", "jest", "tests/unit/value.test.ts"])


def test_workspace_sandbox_allows_only_node_builtin_test_runner(monkeypatch, tmp_path: Path) -> None:
    sandbox = WorkspaceSandbox(tmp_path)
    observed: dict[str, list[str]] = {}

    def fake_run(argv, **_kwargs):
        observed["argv"] = argv

        class Result:
            stdout = "ok"
            stderr = ""
            returncode = 0

        return Result()

    monkeypatch.setattr("app.agents.sandbox.subprocess.run", fake_run)

    result = sandbox.run(["node", "--test", "tests/value.test.js"])

    assert observed["argv"] == ["node", "--test", "tests/value.test.js"]
    assert result.returncode == 0
    with pytest.raises(SandboxViolation, match="only as the built-in test runner"):
        sandbox.run(["node", "scripts/arbitrary.js"])


def test_workspace_sandbox_restores_one_tracked_file_from_head(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    subprocess.run(["git", "init"], cwd=repository, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repository, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repository, check=True)
    tracked = repository / "tracked.txt"
    tracked.write_text("original\n")
    subprocess.run(["git", "add", "tracked.txt"], cwd=repository, check=True)
    subprocess.run(["git", "commit", "-m", "baseline"], cwd=repository, check=True, capture_output=True)
    tracked.unlink()

    sandbox = WorkspaceSandbox(tmp_path)
    sandbox.restore_file("repo/tracked.txt")

    assert tracked.read_text() == "original\n"
    with pytest.raises(SandboxViolation, match="not a tracked file"):
        sandbox.restore_file("repo/untracked.txt")


def test_workspace_sandbox_restores_one_file_from_parent_commit(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    subprocess.run(["git", "init"], cwd=repository, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repository, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repository, check=True)
    tracked = repository / "tracked.txt"
    tracked.write_text("complete\n")
    subprocess.run(["git", "add", "tracked.txt"], cwd=repository, check=True)
    subprocess.run(["git", "commit", "-m", "complete"], cwd=repository, check=True, capture_output=True)
    tracked.write_text("truncated\n")
    subprocess.run(["git", "add", "tracked.txt"], cwd=repository, check=True)
    subprocess.run(["git", "commit", "-m", "broken"], cwd=repository, check=True, capture_output=True)

    sandbox = WorkspaceSandbox(tmp_path)
    sandbox.restore_file("repo/tracked.txt", source="HEAD^")

    assert tracked.read_text() == "complete\n"


def test_sandbox_uses_larger_bounded_file_limit_only_for_build_artifact_commands() -> None:
    assert _command_file_size_limit(["npm", "run", "package:cli"]) == 512 * 1024**2
    assert _command_file_size_limit(["npm", "test"]) == 64 * 1024**2
    assert _command_file_size_limit(["python", "-m", "pytest"]) == 64 * 1024**2


def test_sandbox_limits_cpu_files_and_fds_without_child_address_space_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    limits: list[int] = []
    monkeypatch.setattr(resource, "setrlimit", lambda kind, _value: limits.append(kind))

    _limit_process()

    assert resource.RLIMIT_CPU in limits
    assert resource.RLIMIT_FSIZE in limits
    assert resource.RLIMIT_NOFILE in limits
    assert resource.RLIMIT_AS not in limits


def test_workspace_sandbox_dispatches_command_over_unix_socket(monkeypatch, tmp_path: Path) -> None:
    workspace = tmp_path / "req" / "repo"
    workspace.mkdir(parents=True)
    socket_path = tmp_path / "runtime" / "sandbox.sock"
    socket_path.parent.mkdir()
    monkeypatch.setattr(
        "app.sandbox_executor.get_settings",
        lambda: Settings(_env_file=None, workspace_root=tmp_path),
    )
    server = ThreadingUnixServer(str(socket_path), SandboxRequestHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        sandbox = WorkspaceSandbox(workspace, executor_socket=socket_path)
        result = sandbox.run(["python", "-c", "print('remote-ok')"])
        assert result.returncode == 0
        assert result.output.strip() == "remote-ok"
    finally:
        server.shutdown()
        server.server_close()
