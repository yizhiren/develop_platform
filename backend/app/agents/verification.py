from __future__ import annotations

from pathlib import Path
from typing import Any

from .sandbox import SandboxViolation, WorkspaceSandbox


def run_recorded_tests(context: dict[str, Any], limit: int = 20) -> list[dict[str, Any]]:
    """Re-run the developer's recorded commands with the platform sandbox policy."""
    artifacts = context.get("artifacts", {})
    manifest = (
        artifacts.get("final_verification_manifest")
        or artifacts.get("incremental_verification_manifest")
        or artifacts.get("verification_manifest")
        or artifacts.get("workspace_manifest")
        or {}
    )
    report = artifacts.get("development_report") or {}
    root = manifest.get("workspace_root")
    recorded = report.get("tests") or []
    if not root or not recorded:
        return []
    sandbox = WorkspaceSandbox(Path(root))
    results: list[dict[str, Any]] = []
    for item in recorded[:limit]:
        argv = item.get("command") if isinstance(item, dict) else None
        cwd = item.get("cwd", ".") if isinstance(item, dict) else "."
        if not isinstance(argv, list) or not argv or not all(isinstance(value, str) for value in argv):
            results.append({"command": argv, "cwd": cwd, "status": "failed", "error": "invalid recorded command"})
            continue
        try:
            completed = sandbox.run(argv, cwd)
            results.append(
                {
                    "command": completed.argv,
                    "cwd": cwd,
                    "status": "passed" if completed.returncode == 0 else "failed",
                    "returncode": completed.returncode,
                    "output": completed.output[:8_000],
                    "truncated": completed.truncated,
                }
            )
        except (SandboxViolation, OSError, ValueError) as exc:
            results.append({"command": argv, "cwd": cwd, "status": "failed", "error": str(exc)})
    return results
