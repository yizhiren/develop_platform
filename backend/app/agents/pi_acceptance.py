from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from ..schemas.domain import AcceptanceReport
from .acceptance import (
    ACCEPTANCE_ROLES,
    AcceptanceSpecInvalid,
    _acceptance_criteria,
    _command_completed_successfully,
    _compact_context,
    _is_open_acceptance_command,
    _workspace_relative_path,
    verification_manifest,
)
from .coding import _is_validation_command
from .pi_bridge import PiAgentCoreBridge, PiToolDefinition, PiToolResult
from .providers import ModelResponse, OpenAICompatibleProvider
from .runtime import AgentOutputError
from .sandbox import SandboxViolation, WorkspaceSandbox


class PiAcceptanceToolLoop:
    """Persistent Pi Agent4 loop in a clean verification workspace."""

    def __init__(
        self,
        provider: OpenAICompatibleProvider,
        bridge: PiAgentCoreBridge,
        role: str,
        max_turns: int = 32,
    ) -> None:
        if role not in ACCEPTANCE_ROLES:
            raise ValueError(f"unsupported acceptance role: {role}")
        self.provider = provider
        self.bridge = bridge
        self.role = role
        self.max_turns = min(max(max_turns, 4), 50)

    async def run(self, context: dict[str, Any]) -> tuple[AcceptanceReport, ModelResponse]:
        manifest = verification_manifest(context, self.role)
        if not manifest or not manifest.get("workspace_root"):
            raise AgentOutputError("verification workspace manifest is missing")
        criteria = _acceptance_criteria(context)
        expected_ids = [str(item.get("id", "")).strip() for item in criteria]
        if (
            not expected_ids
            or any(not item for item in expected_ids)
            or len(set(expected_ids)) != len(expected_ids)
        ):
            raise AcceptanceSpecInvalid(
                "confirmed acceptance criteria must contain unique, non-empty ids"
            )

        sandbox = WorkspaceSandbox(Path(str(manifest["workspace_root"])))
        evidence: list[dict[str, Any]] = []
        final_report: AcceptanceReport | None = None

        async def handle_tool(name: str, arguments: dict[str, Any]) -> PiToolResult:
            nonlocal final_report
            if name == "list_files":
                requested = _workspace_relative_path(str(arguments.get("path") or "."), manifest)
                files = await asyncio.to_thread(sandbox.list_files, requested, 300)
                return PiToolResult(observation={"type": "files", "files": files})
            if name in {"read_file", "run_command"}:
                criterion_ids = _criterion_ids(arguments, expected_ids)
            else:
                criterion_ids = []
            if name == "read_file":
                requested = _workspace_relative_path(str(arguments.get("path") or ""), manifest)
                if not requested:
                    raise ValueError("read_file requires path")
                content = await asyncio.to_thread(sandbox.read_file, requested)
                evidence_item = {
                    "evidence_id": f"agent4-read-{len(evidence) + 1}",
                    "source": "agent4_independent",
                    "type": "file_inspection",
                    "status": "passed",
                    "criterion_ids": criterion_ids,
                    "path": requested,
                }
                evidence.append(evidence_item)
                return PiToolResult(
                    observation={
                        "type": "file",
                        "path": requested,
                        "content": content[:80_000],
                        "evidence_id": evidence_item["evidence_id"],
                    }
                )
            if name == "run_command":
                argv = arguments.get("argv")
                if not isinstance(argv, list) or not argv or not all(
                    isinstance(item, str) for item in argv
                ):
                    raise ValueError("run_command requires a non-empty string argv")
                is_validation = _is_validation_command(argv)
                if not is_validation and not _is_open_acceptance_command(argv):
                    raise ValueError(
                        "command must use an allowed validation executable, npm, grep, or rg"
                    )
                cwd = str(arguments.get("cwd") or ".")
                result = await asyncio.to_thread(sandbox.run, argv, cwd)
                status = (
                    "passed"
                    if _command_completed_successfully(argv, result.returncode)
                    else "failed"
                )
                evidence_item = {
                    "evidence_id": f"agent4-test-{len(evidence) + 1}",
                    "source": "agent4_independent",
                    "type": "command",
                    "status": status,
                    "criterion_ids": criterion_ids,
                    "command": result.argv,
                    "cwd": cwd,
                    "returncode": result.returncode,
                    "output": result.output[:8_000],
                    "truncated": result.truncated,
                }
                evidence.append(evidence_item)
                return PiToolResult(
                    observation={
                        "type": "command",
                        "evidence_id": evidence_item["evidence_id"],
                        "argv": result.argv,
                        "returncode": result.returncode,
                        "output": result.output,
                        "truncated": result.truncated,
                        "status": status,
                    }
                )
            if name == "finish_acceptance":
                raw_report = arguments.get("report")
                if not isinstance(raw_report, dict):
                    raise ValueError("finish_acceptance requires a report object")
                try:
                    report = AcceptanceReport.model_validate(
                        {
                            **raw_report,
                            "regression_results": raw_report.get("regression_results", []),
                            "environment": raw_report.get("environment", {}),
                        }
                    )
                except ValidationError as exc:
                    raise ValueError(_validation_message(exc)) from exc
                report.regression_results = evidence
                report.environment = {
                    **report.environment,
                    "checkout_type": str(manifest.get("checkout_type", "verification")),
                    "workspace": "clean_sha_verified_checkout",
                    "network": "enabled",
                    "executor": "sandbox",
                    "agent_core": "pi",
                }
                final_report = report
                return PiToolResult(
                    observation={
                        "type": "acceptance_submitted",
                        "approved": report.approved,
                    },
                    terminate=True,
                )
            raise ValueError("unsupported acceptance tool")

        safe_context = _compact_context(context, manifest)
        safe_context["acceptance_progress"] = {
            "expected_criterion_ids": expected_ids,
        }
        system_prompt = (
            "你是独立验收工程师（Agent4），通过 Pi Agent Core 在干净、已校验 SHA 的"
            "工作区中自主验收。请自行调查代码、选择验证方法和运行所需命令，并在最多"
            f" {self.max_turns} 轮内完成调查、调用 finish_acceptance 给出明确结论。"
            "read_file 和 run_command 可填写它们直接覆盖的 criterion_ids，便于记录审计日志。"
            "验收环境允许访问公网；run_command 可执行任意 npm 子命令以及 grep/rg。"
            "不得访问凭据、.git 或平台服务。每个 passed 项必须引用平台返回的"
            " evidence_id；由你根据调查和命令结果决定是否批准。"
            "regression_results 与 environment 由平台记录工具执行事实，无需在工具参数中提交。"
        )
        response = await self.bridge.run(
            provider=self.provider,
            system_prompt=system_prompt,
            user_prompt=json.dumps(safe_context, ensure_ascii=False),
            tools=_acceptance_tools(),
            terminal_tools={"finish_acceptance"},
            handler=handle_tool,
            max_turns=self.max_turns,
        )
        if final_report is None:
            raise AgentOutputError("Pi acceptance stopped without a valid report")
        response.content = final_report.model_dump_json()
        return final_report, response


def _acceptance_tools() -> list[PiToolDefinition]:
    report_schema = AcceptanceReport.model_json_schema()
    report_schema["properties"].pop("regression_results", None)
    report_schema["properties"].pop("environment", None)
    report_schema["required"] = [
        item
        for item in report_schema.get("required", [])
        if item not in {"regression_results", "environment"}
    ]
    definitions = report_schema.pop("$defs", {})
    finish_parameters: dict[str, Any] = {
        "type": "object",
        "properties": {"report": report_schema},
        "required": ["report"],
        "additionalProperties": False,
    }
    if definitions:
        finish_parameters["$defs"] = definitions
    criterion_ids = {
        "type": "array",
        "items": {"type": "string"},
        "uniqueItems": True,
    }
    return [
        PiToolDefinition(
            "list_files",
            "列出验收文件",
            "列出干净验收工作区文件。",
            {
                "type": "object",
                "properties": {"path": {"type": "string", "default": "."}},
                "additionalProperties": False,
            },
        ),
        PiToolDefinition(
            "read_file",
            "读取验收文件",
            "读取验收工作区中的文件并生成验收记录。",
            {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "minLength": 1},
                    "criterion_ids": criterion_ids,
                },
                "required": ["path"],
                "additionalProperties": False,
            },
        ),
        PiToolDefinition(
            "run_command",
            "运行独立验收命令",
            "运行任意 npm 命令、grep/rg 搜索，或其他受白名单约束的验证命令。",
            {
                "type": "object",
                "properties": {
                    "argv": {
                        "type": "array",
                        "items": {"type": "string"},
                        "minItems": 1,
                    },
                    "cwd": {"type": "string", "default": "."},
                    "criterion_ids": criterion_ids,
                },
                "required": ["argv"],
                "additionalProperties": False,
            },
        ),
        PiToolDefinition(
            "finish_acceptance",
            "提交验收报告",
            "证据充分后提交完整 AcceptanceReport。",
            finish_parameters,
        ),
    ]


def _criterion_ids(arguments: dict[str, Any], expected_ids: list[str]) -> list[str]:
    raw = arguments.get("criterion_ids")
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError("criterion_ids 必须是字符串数组")
    values = [str(item) for item in raw]
    invalid = sorted(set(values) - set(expected_ids))
    if invalid:
        raise ValueError("criterion_ids 包含未知 id: " + ", ".join(invalid))
    return sorted(set(values))


def _validation_message(exc: ValidationError) -> str:
    issues = []
    for item in exc.errors(include_url=False, include_input=False)[:8]:
        location = ".".join(str(part) for part in item.get("loc", ())) or "$"
        issues.append(f"{location}: {item.get('msg', 'invalid value')}")
    return "AcceptanceReport 校验失败: " + "; ".join(issues)


__all__ = ["PiAcceptanceToolLoop"]
