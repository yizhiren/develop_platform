from pathlib import Path
import threading

import pytest

from app.agents.sandbox import SandboxViolation, WorkspaceSandbox
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


def test_workspace_sandbox_blocks_escape_and_package_install(tmp_path: Path) -> None:
    sandbox = WorkspaceSandbox(tmp_path)
    with pytest.raises(SandboxViolation, match="escapes"):
        sandbox.read_file("../secret")
    with pytest.raises(SandboxViolation, match="not allowed"):
        sandbox.run(["npm", "install"])


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
