from __future__ import annotations

import json
import os
import socketserver
from pathlib import Path
from typing import Any

from .agents.sandbox import SandboxViolation, WorkspaceSandbox
from .core.config import get_settings


MAX_REQUEST_BYTES = 256_000


class SandboxRequestHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        try:
            raw = self.rfile.readline(MAX_REQUEST_BYTES + 1)
            if len(raw) > MAX_REQUEST_BYTES or not raw.endswith(b"\n"):
                raise SandboxViolation("sandbox request exceeds protocol limit")
            request: dict[str, Any] = json.loads(raw)
            workspace_root = Path(request["workspace_root"]).resolve()
            allowed_root = get_settings().workspace_root.resolve()
            if not workspace_root.is_relative_to(allowed_root):
                raise SandboxViolation("remote workspace is outside the allowed root")
            sandbox = WorkspaceSandbox(workspace_root, executor_socket=None, discover_executor=False)
            result = sandbox.run(
                list(request["argv"]),
                str(request.get("cwd", ".")),
                int(request.get("timeout", 300)),
            )
            response = {
                "ok": True,
                "result": {
                    "argv": result.argv,
                    "returncode": result.returncode,
                    "output": result.output,
                    "truncated": result.truncated,
                },
            }
        except Exception as exc:
            response = {"ok": False, "error": str(exc)[:2000]}
        self.wfile.write(json.dumps(response, ensure_ascii=False).encode() + b"\n")


class ThreadingUnixServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


def main() -> None:
    settings = get_settings()
    socket_path = settings.sandbox_executor_socket
    if socket_path is None:
        raise RuntimeError("SANDBOX_EXECUTOR_SOCKET is required")
    socket_path.parent.mkdir(parents=True, exist_ok=True)
    socket_path.unlink(missing_ok=True)
    with ThreadingUnixServer(str(socket_path), SandboxRequestHandler) as server:
        os.chmod(socket_path, 0o660)
        server.serve_forever()


if __name__ == "__main__":
    main()
