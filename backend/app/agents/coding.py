from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError

from ..schemas.domain import DevelopmentReport
from .providers import LLMProvider, ModelResponse
from .runtime import AgentOutputError
from .sandbox import SandboxViolation, WorkspaceSandbox


class DeveloperAction(BaseModel):
    action: Literal["list_files", "read_file", "write_file", "replace_text", "delete_file", "run_command", "finish"]
    path: str | None = None
    content: str | None = None
    old: str | None = None
    new: str | None = None
    argv: list[str] | None = None
    cwd: str = "."
    report: DevelopmentReport | None = None


class DeveloperToolLoop:
    def __init__(self, provider: LLMProvider, max_steps: int = 30):
        self.provider = provider
        self.max_steps = max_steps

    async def run(self, context: dict[str, Any]) -> tuple[DevelopmentReport, ModelResponse]:
        manifest = context.get("artifacts", {}).get("workspace_manifest")
        if not manifest or not manifest.get("workspace_root"):
            raise AgentOutputError("developer workspace manifest is missing")
        sandbox = WorkspaceSandbox(Path(manifest["workspace_root"]))
        action_schema = json.dumps(DeveloperAction.model_json_schema(), ensure_ascii=False)
        system = (
            "你是开发工程师，在隔离工作区内完成已确认方案。每次只输出一个符合 DeveloperAction Schema 的 JSON 对象。"
            "先检查仓库，再修改实现与测试；不得访问凭据、网络、.git 或平台服务。至少运行一个相关测试且成功后才能 finish。"
            "使用相对 workspace_root 的路径；多个仓库位于各自 repository_id 目录。finish.report 必须准确描述实际改动和测试。"
            f"\nDeveloperAction JSON Schema:\n{action_schema}"
        )
        safe_context = {
            "requirement_id": context.get("requirement_id"),
            "title": context.get("title"),
            "description": context.get("description"),
            "repositories": manifest.get("repositories", []),
            "artifacts": {
                key: value
                for key, value in context.get("artifacts", {}).items()
                if key in {"clarification_spec", "architecture_plan", "architecture_revision", "code_review_report", "acceptance_report"}
            },
        }
        observation: dict[str, Any] = {"type": "workspace", "files": sandbox.list_files(limit=300)}
        transcript: list[dict[str, Any]] = []
        prompt_tokens = completion_tokens = 0
        model = ""
        successful_tests: list[dict[str, Any]] = []
        for step in range(1, self.max_steps + 1):
            user_payload = {
                "context": safe_context,
                "recent_transcript": transcript[-8:],
                "observation": observation,
                "step": step,
                "steps_remaining": self.max_steps - step + 1,
            }
            response = await self.provider.complete(system, json.dumps(user_payload, ensure_ascii=False))
            prompt_tokens += response.prompt_tokens
            completion_tokens += response.completion_tokens
            model = response.model or model
            try:
                action = DeveloperAction.model_validate_json(response.content)
            except ValidationError as exc:
                observation = {"type": "error", "message": "输出不符合 DeveloperAction JSON Schema"}
                transcript.append({"action": "invalid_json"})
                if step == self.max_steps:
                    raise AgentOutputError("developer tool loop returned invalid actions") from exc
                continue
            try:
                observation = self._execute(action, sandbox)
            except (SandboxViolation, OSError, ValueError) as exc:
                observation = {"type": "tool_error", "action": action.action, "message": str(exc)}
            transcript.append({"action": action.action, "observation": _truncate(observation)})
            if action.action == "run_command" and observation.get("returncode") == 0:
                successful_tests.append(
                    {"command": action.argv, "cwd": action.cwd, "status": "passed", "output": observation.get("output", "")[:4000]}
                )
            if action.action == "finish":
                if action.report is None:
                    observation = {"type": "error", "message": "finish 必须包含 report"}
                    continue
                if not successful_tests:
                    observation = {"type": "error", "message": "finish 前必须至少运行一个成功的测试命令"}
                    continue
                report = action.report.model_copy(deep=True)
                report.tests = successful_tests
                if not report.repositories_changed:
                    observation = {"type": "error", "message": "report.repositories_changed 不能为空"}
                    continue
                return report, ModelResponse(
                    content=report.model_dump_json(),
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    model=model,
                )
        raise AgentOutputError("developer tool loop exhausted its step budget")

    @staticmethod
    def _execute(action: DeveloperAction, sandbox: WorkspaceSandbox) -> dict[str, Any]:
        if action.action == "list_files":
            return {"type": "files", "files": sandbox.list_files(action.path or ".")}
        if action.action == "read_file":
            if not action.path:
                raise ValueError("read_file requires path")
            return {"type": "file", "path": action.path, "content": sandbox.read_file(action.path)[:80_000]}
        if action.action == "write_file":
            if not action.path or action.content is None:
                raise ValueError("write_file requires path and content")
            sandbox.write_file(action.path, action.content)
            return {"type": "write", "path": action.path, "bytes": len(action.content.encode())}
        if action.action == "replace_text":
            if not action.path or action.old is None or action.new is None:
                raise ValueError("replace_text requires path, old and new")
            sandbox.replace_text(action.path, action.old, action.new)
            return {"type": "replace", "path": action.path}
        if action.action == "delete_file":
            if not action.path:
                raise ValueError("delete_file requires path")
            sandbox.delete_file(action.path)
            return {"type": "delete", "path": action.path}
        if action.action == "run_command":
            if not action.argv:
                raise ValueError("run_command requires argv")
            result = sandbox.run(action.argv, action.cwd)
            return {"type": "command", "argv": result.argv, "returncode": result.returncode, "output": result.output, "truncated": result.truncated}
        if action.action == "finish":
            return {"type": "finish"}
        raise ValueError("unsupported developer action")


def _truncate(value: dict[str, Any], limit: int = 24_000) -> dict[str, Any]:
    encoded = json.dumps(value, ensure_ascii=False)
    if len(encoded) <= limit:
        return value
    return {"type": value.get("type", "observation"), "truncated": True, "preview": encoded[:limit]}
