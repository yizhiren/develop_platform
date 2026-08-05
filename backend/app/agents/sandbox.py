from __future__ import annotations

import os
import resource
import json
import socket
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path


class SandboxViolation(ValueError):
    pass


@dataclass(frozen=True)
class CommandResult:
    argv: list[str]
    returncode: int
    output: str
    truncated: bool


class WorkspaceSandbox:
    """Constrained filesystem/command surface exposed to the developer Agent."""

    ALLOWED_EXECUTABLES = {"pytest", "python", "python3", "npm", "pnpm", "yarn", "go", "cargo", "make"}
    BLOCKED_ARGUMENTS = {"install", "add", "publish", "login", "config"}

    def __init__(
        self,
        root: Path,
        max_file_bytes: int = 256_000,
        max_output_bytes: int = 128_000,
        executor_socket: Path | None = None,
        discover_executor: bool = True,
    ):
        self.root = root.resolve()
        self.max_file_bytes = max_file_bytes
        self.max_output_bytes = max_output_bytes
        configured_socket = os.environ.get("SANDBOX_EXECUTOR_SOCKET") if discover_executor else None
        self.executor_socket = executor_socket or (Path(configured_socket) if configured_socket else None)
        if not self.root.is_dir():
            raise SandboxViolation("workspace root does not exist")

    def list_files(self, relative: str = ".", limit: int = 500) -> list[str]:
        base = self._path(relative)
        if not base.is_dir():
            raise SandboxViolation("list target is not a directory")
        result: list[str] = []
        for path in sorted(base.rglob("*")):
            if path.is_file() and ".git" not in path.relative_to(self.root).parts:
                result.append(path.relative_to(self.root).as_posix())
                if len(result) >= limit:
                    break
        return result

    def read_file(self, relative: str) -> str:
        path = self._path(relative)
        if not path.is_file() or path.stat().st_size > self.max_file_bytes:
            raise SandboxViolation("file is missing or exceeds read limit")
        return path.read_text(encoding="utf-8", errors="replace")

    def write_file(self, relative: str, content: str) -> None:
        encoded = content.encode()
        if len(encoded) > self.max_file_bytes:
            raise SandboxViolation("file exceeds write limit")
        path = self._path(relative)
        if ".git" in path.relative_to(self.root).parts:
            raise SandboxViolation("Git metadata is read-only to agents")
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.forgeflow-tmp")
        temporary.write_bytes(encoded)
        temporary.replace(path)

    def replace_text(self, relative: str, old: str, new: str) -> None:
        if not old:
            raise SandboxViolation("replacement source cannot be empty")
        content = self.read_file(relative)
        if content.count(old) != 1:
            raise SandboxViolation("replacement source must occur exactly once")
        self.write_file(relative, content.replace(old, new, 1))

    def delete_file(self, relative: str) -> None:
        path = self._path(relative)
        if not path.is_file() or ".git" in path.relative_to(self.root).parts:
            raise SandboxViolation("delete target is not an allowed file")
        path.unlink()

    def run(self, argv: list[str], cwd: str = ".", timeout: int = 300) -> CommandResult:
        if not argv or Path(argv[0]).name not in self.ALLOWED_EXECUTABLES:
            raise SandboxViolation("command is not allowlisted")
        if any(argument.lower() in self.BLOCKED_ARGUMENTS for argument in argv[1:3]):
            raise SandboxViolation("dependency or registry mutation is not allowed")
        working_directory = self._path(cwd)
        if not working_directory.is_dir():
            raise SandboxViolation("command working directory is invalid")
        if self.executor_socket is not None:
            return self._run_remote(argv, cwd, timeout)
        environment = {
            "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
            "HOME": "/tmp/forgeflow-agent-home",
            "CI": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "NO_COLOR": "1",
        }
        try:
            completed = subprocess.run(
                argv,
                cwd=working_directory,
                env=environment,
                capture_output=True,
                text=True,
                errors="replace",
                timeout=min(max(timeout, 1), 900),
                check=False,
                preexec_fn=_limit_process,
            )
        except subprocess.TimeoutExpired as exc:
            raise SandboxViolation(f"command timed out after {exc.timeout} seconds") from exc
        output = completed.stdout + completed.stderr
        encoded = output.encode(errors="replace")
        truncated = len(encoded) > self.max_output_bytes
        if truncated:
            output = encoded[: self.max_output_bytes].decode(errors="replace") + "\n[output truncated]"
        return CommandResult(argv=argv, returncode=completed.returncode, output=output, truncated=truncated)

    def _run_remote(self, argv: list[str], cwd: str, timeout: int) -> CommandResult:
        request = json.dumps(
            {
                "workspace_root": str(self.root),
                "argv": argv,
                "cwd": cwd,
                "timeout": min(max(timeout, 1), 900),
            },
            ensure_ascii=False,
        ).encode() + b"\n"
        last_error: OSError | None = None
        for _attempt in range(20):
            try:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                    client.settimeout(min(max(timeout + 10, 10), 910))
                    client.connect(str(self.executor_socket))
                    client.sendall(request)
                    response_file = client.makefile("rb")
                    # JSON escaping can expand control characters well beyond the
                    # already-truncated command output size.
                    raw = response_file.readline(self.max_output_bytes * 8 + 65_536)
                if not raw.endswith(b"\n"):
                    raise SandboxViolation("sandbox executor response exceeds protocol limit")
                response = json.loads(raw)
                if not response.get("ok"):
                    raise SandboxViolation(str(response.get("error", "sandbox executor failed")))
                result = response["result"]
                return CommandResult(
                    argv=list(result["argv"]),
                    returncode=int(result["returncode"]),
                    output=str(result["output"]),
                    truncated=bool(result["truncated"]),
                )
            except (FileNotFoundError, ConnectionRefusedError) as exc:
                last_error = exc
                time.sleep(0.1)
        raise SandboxViolation("sandbox executor socket is unavailable") from last_error

    def _path(self, relative: str) -> Path:
        candidate = (self.root / relative).resolve()
        try:
            candidate.relative_to(self.root)
        except ValueError as exc:
            raise SandboxViolation("path escapes workspace") from exc
        return candidate


def _limit_process() -> None:
    resource.setrlimit(resource.RLIMIT_CPU, (300, 300))
    resource.setrlimit(resource.RLIMIT_AS, (2 * 1024**3, 2 * 1024**3))
    resource.setrlimit(resource.RLIMIT_FSIZE, (64 * 1024**2, 64 * 1024**2))
    resource.setrlimit(resource.RLIMIT_NOFILE, (256, 256))
