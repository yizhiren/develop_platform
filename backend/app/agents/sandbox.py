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

    ALLOWED_EXECUTABLES = {
        "pytest",
        "python",
        "python3",
        "npm",
        "pnpm",
        "yarn",
        "npx",
        "node",
        "go",
        "cargo",
        "make",
        "grep",
        "rg",
    }
    ALLOWED_NPX_TOOLS = {"tsc", "eslint", "vitest", "jest", "prettier"}
    BLOCKED_ARGUMENTS = {
        "install",
        "ci",
        "add",
        "update",
        "upgrade",
        "remove",
        "uninstall",
        "prune",
        "rebuild",
        "publish",
        "login",
        "config",
    }
    SEARCH_EXCLUDED_DIRECTORIES = {
        ".git",
        ".next",
        ".pytest_cache",
        ".venv",
        "__pycache__",
        "coverage",
        "node_modules",
        "venv",
    }

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

    def search_text(
        self,
        query: str,
        relative: str = ".",
        limit: int = 100,
    ) -> list[dict[str, str | int]]:
        """Search bounded workspace text files for a literal string."""
        if not query or len(query.encode()) > 1_000:
            raise SandboxViolation("search query must be between 1 and 1000 bytes")
        if limit < 1 or limit > 500:
            raise SandboxViolation("search result limit must be between 1 and 500")
        base = self._path(relative)
        if not base.exists():
            raise SandboxViolation("search target does not exist")

        candidates: list[Path] = []
        if base.is_file() and not base.is_symlink():
            candidates.append(base)
        elif base.is_dir():
            for directory, names, filenames in os.walk(base, followlinks=False):
                names[:] = sorted(
                    name
                    for name in names
                    if name not in self.SEARCH_EXCLUDED_DIRECTORIES
                    and not (Path(directory) / name).is_symlink()
                )
                for filename in sorted(filenames):
                    path = Path(directory) / filename
                    if not path.is_symlink():
                        candidates.append(path)
        else:
            raise SandboxViolation("search target is not a regular file or directory")

        matches: list[dict[str, str | int]] = []
        for path in candidates:
            try:
                if not path.is_file() or path.stat().st_size > self.max_file_bytes:
                    continue
                content = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if "\x00" in content:
                continue
            for line_number, line in enumerate(content.splitlines(), start=1):
                if query not in line:
                    continue
                matches.append(
                    {
                        "path": path.relative_to(self.root).as_posix(),
                        "line": line_number,
                        "text": line[:500],
                    }
                )
                if len(matches) >= limit:
                    return matches
        return matches

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
        match_count = content.count(old)
        if match_count == 0 and new and content.count(new) == 1:
            # Model retries can replay a mutation after a stale observation or
            # response timeout. An already-applied unique replacement is a
            # successful no-op, while ambiguous state still fails below.
            return
        if match_count != 1:
            raise SandboxViolation(
                f"replacement source matched {match_count} times; it must match exactly once. "
                "Read the latest file and include more surrounding context, or use read_lines followed by replace_lines."
            )
        self.write_file(relative, content.replace(old, new, 1))

    def replace_lines(self, relative: str, start_line: int, end_line: int, new: str) -> None:
        content = self.read_file(relative)
        lines = content.splitlines(keepends=True)
        if start_line < 1 or end_line < start_line or end_line > len(lines):
            raise SandboxViolation(
                f"line range {start_line}-{end_line} is outside file bounds 1-{len(lines)}"
            )
        line_ending = "\r\n" if "\r\n" in content else "\n"
        replacement = new
        replaced_section_had_newline = bool(lines[end_line - 1].endswith(("\n", "\r")))
        has_following_lines = end_line < len(lines)
        if replacement and not replacement.endswith(("\n", "\r")) and (
            replaced_section_had_newline or has_following_lines
        ):
            replacement += line_ending
        updated = "".join(lines[: start_line - 1]) + replacement + "".join(lines[end_line:])
        self.write_file(relative, updated)

    def delete_file(self, relative: str) -> bool:
        path = self._path(relative)
        if ".git" in path.relative_to(self.root).parts:
            raise SandboxViolation("delete target is not an allowed file")
        if not path.exists():
            return False
        if not path.is_file() or path.is_symlink():
            raise SandboxViolation("delete target is not an allowed file")
        path.unlink()
        return True

    def restore_file(self, relative: str, source: str = "HEAD") -> None:
        """Restore one tracked file from a fixed, trusted repository revision."""
        if source not in {"HEAD", "HEAD^"}:
            raise SandboxViolation("restore source is not allowed")
        path = self._path(relative)
        if ".git" in path.relative_to(self.root).parts:
            raise SandboxViolation("Git metadata is read-only to agents")
        repository = path.parent
        while repository != self.root.parent and not (repository / ".git").exists():
            repository = repository.parent
        if repository == self.root.parent or not (repository / ".git").exists():
            raise SandboxViolation("restore target is not inside a Git repository")
        repository_relative = path.relative_to(repository).as_posix()
        completed = subprocess.run(
            ["git", "restore", f"--source={source}", "--", repository_relative],
            cwd=repository,
            env={
                "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
                "HOME": "/tmp/forgeflow-agent-home",
                "GIT_CONFIG_NOSYSTEM": "1",
            },
            capture_output=True,
            text=True,
            errors="replace",
            timeout=30,
            check=False,
        )
        if completed.returncode != 0 or not path.is_file():
            detail = (completed.stdout + completed.stderr).strip()
            raise SandboxViolation(
                "restore target is not a tracked file in the current HEAD"
                + (f": {detail[:500]}" if detail else "")
            )

    def run(self, argv: list[str], cwd: str = ".", timeout: int = 300) -> CommandResult:
        executable = Path(argv[0]).name if argv else ""
        if not argv or executable not in self.ALLOWED_EXECUTABLES:
            raise SandboxViolation("command is not allowlisted")
        if executable == "node" and (len(argv) < 2 or argv[1] != "--test"):
            raise SandboxViolation(
                "node is allowed only as the built-in test runner (`node --test ...`); "
                "use an existing npm/pnpm/yarn script for other repository commands"
            )
        if executable in {"pnpm", "yarn"} and len(argv) > 1 and argv[1].lower() in {"exec", "dlx"}:
            raise SandboxViolation(
                "package-manager exec commands are not allowed; use a repository validation script "
                "or an allowlisted local-only npx tool"
            )
        if executable != "npm" and any(
            argument.lower() in self.BLOCKED_ARGUMENTS for argument in argv[1:3]
        ):
            raise SandboxViolation("dependency or registry mutation is not allowed")
        working_directory = self._path(cwd)
        if not working_directory.is_dir():
            raise SandboxViolation("command working directory is invalid")
        if executable == "npx":
            tool = next((item for item in argv[1:] if not item.startswith("-")), "")
            if tool not in self.ALLOWED_NPX_TOOLS or "@" in tool:
                raise SandboxViolation("npx tool is not allowlisted")
            search_roots = [working_directory, *working_directory.parents]
            if not any(
                root.is_relative_to(self.root) and (root / "node_modules" / ".bin" / tool).is_file()
                for root in search_roots
            ):
                raise SandboxViolation("npx tool is not installed in the workspace")
            if "--no-install" not in argv[1:]:
                argv = [argv[0], "--no-install", *argv[1:]]
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
            head_bytes = self.max_output_bytes // 2
            tail_bytes = self.max_output_bytes - head_bytes
            output = (
                encoded[:head_bytes].decode(errors="replace")
                + "\n[output truncated; final output follows]\n"
                + encoded[-tail_bytes:].decode(errors="replace")
            )
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
        raw = Path(relative)
        if raw.is_absolute() or ".git" in raw.parts:
            raise SandboxViolation("Git metadata and absolute paths are not accessible to agents")
        candidate = (self.root / relative).resolve()
        try:
            resolved_relative = candidate.relative_to(self.root)
        except ValueError as exc:
            raise SandboxViolation("path escapes workspace") from exc
        if ".git" in resolved_relative.parts:
            raise SandboxViolation("Git metadata and absolute paths are not accessible to agents")
        return candidate


def _limit_process() -> None:
    resource.setrlimit(resource.RLIMIT_CPU, (300, 300))
    resource.setrlimit(resource.RLIMIT_NOFILE, (256, 256))
