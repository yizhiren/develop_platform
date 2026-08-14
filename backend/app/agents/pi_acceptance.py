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
    _baseline_evidence,
    _changed_initial_files,
    _command_completed_successfully,
    _compact_context,
    _normalize_finish_report,
    _is_open_acceptance_command,
    _platform_evidence_report,
    _report_issues,
    _snapshot_initial_files,
    _workspace_relative_path,
    verification_manifest,
)
from .coding import _is_validation_command
from .pi_bridge import PiAgentCoreBridge, PiToolDefinition, PiToolResult
from .providers import ModelResponse, OpenAICompatibleProvider
from .runtime import AgentOutputError
from .sandbox import SandboxViolation, WorkspaceSandbox


class PiAcceptanceToolLoop:
    """Persistent Pi Agent4 loop with the existing platform evidence gates."""

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
        baseline_hashes = await asyncio.to_thread(_snapshot_initial_files, sandbox)
        evidence = _baseline_evidence(context.get("runtime_verification", []))
        evidence_by_id = {str(item["evidence_id"]): item for item in evidence}
        covered_ids: set[str] = set()
        independent_test_count = 0
        failed_test_count = sum(1 for item in evidence if item.get("status") != "passed")
        integrity_violations: list[str] = []
        action_counts: dict[str, int] = {}
        exploration_actions = 0
        rejected_finishes = 0
        final_report: AcceptanceReport | None = None

        async def handle_tool(name: str, arguments: dict[str, Any]) -> PiToolResult:
            nonlocal independent_test_count, failed_test_count, integrity_violations
            nonlocal exploration_actions, rejected_finishes, final_report
            signature = _tool_signature(name, arguments)
            action_counts[signature] = action_counts.get(signature, 0) + 1
            if action_counts[signature] > 2:
                raise ValueError("重复工具调用已被平台阻止；请使用已有证据继续验收")
            if name in {"list_files", "read_file"}:
                exploration_actions += 1
                if exploration_actions > 12:
                    raise ValueError("只读调查预算已耗尽；请运行必要验证并提交报告")
            if integrity_violations and name != "finish_acceptance":
                raise ValueError(
                    "验收命令改变了初始工作区内容；只能提交 approved=false 的报告"
                )
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
                if not requested or requested not in baseline_hashes:
                    raise ValueError(
                        "read_file 只能读取初始干净 checkout 中存在的文件"
                    )
                content = await asyncio.to_thread(sandbox.read_file, requested)
                evidence_item = {
                    "evidence_id": f"agent4-read-{len(evidence_by_id) + 1}",
                    "source": "agent4_independent",
                    "type": "file_inspection",
                    "status": "passed",
                    "criterion_ids": criterion_ids,
                    "path": requested,
                    "sha256": baseline_hashes[requested],
                }
                evidence.append(evidence_item)
                evidence_by_id[str(evidence_item["evidence_id"])] = evidence_item
                covered_ids.update(criterion_ids)
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
                changed = await asyncio.to_thread(
                    _changed_initial_files,
                    sandbox,
                    baseline_hashes,
                )
                if changed:
                    integrity_violations = changed[:20]
                status = (
                    "passed"
                    if _command_completed_successfully(argv, result.returncode) and not changed
                    else "failed"
                )
                evidence_item = {
                    "evidence_id": f"agent4-test-{len(evidence_by_id) + 1}",
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
                if changed:
                    evidence_item["workspace_integrity_violations"] = changed[:20]
                evidence.append(evidence_item)
                evidence_by_id[str(evidence_item["evidence_id"])] = evidence_item
                covered_ids.update(criterion_ids)
                if status == "passed" and is_validation:
                    independent_test_count += 1
                else:
                    failed_test_count += 1
                return PiToolResult(
                    observation={
                        "type": "command",
                        "evidence_id": evidence_item["evidence_id"],
                        "argv": result.argv,
                        "returncode": result.returncode,
                        "output": result.output,
                        "truncated": result.truncated,
                        "status": status,
                        "workspace_integrity_violations": changed[:20],
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
                report = _normalize_finish_report(
                    report,
                    criteria,
                    evidence_by_id,
                    independent_test_count,
                    failed_test_count,
                    integrity_violations,
                )
                issues = _report_issues(
                    report,
                    criteria,
                    evidence_by_id,
                    independent_test_count,
                    failed_test_count,
                    integrity_violations,
                )
                if issues:
                    rejected_finishes += 1
                    if rejected_finishes >= 2:
                        report = _platform_evidence_report(
                            criteria,
                            evidence_by_id,
                            independent_test_count,
                            failed_test_count,
                            integrity_violations,
                        )
                        issues = _report_issues(
                            report,
                            criteria,
                            evidence_by_id,
                            independent_test_count,
                            failed_test_count,
                            integrity_violations,
                        )
                if issues:
                    raise ValueError("验收报告未通过平台门禁: " + "; ".join(issues[:8]))
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
                        "covered_criterion_ids": sorted(covered_ids),
                    },
                    terminate=True,
                )
            raise ValueError("unsupported acceptance tool")

        safe_context = _compact_context(context, manifest)
        safe_context["acceptance_progress"] = {
            "expected_criterion_ids": expected_ids,
            "baseline_evidence": evidence,
            "initial_files": sorted(baseline_hashes)[:300],
        }
        system_prompt = (
            "你是独立验收工程师（Agent4），通过 Pi Agent Core 在干净、已校验 SHA 的"
            "工作区中逐项验收。先读取与每个验收标准相关的真实实现或测试，再独立运行至少一个"
            "有失败条件的验证命令。read_file 和 run_command 必须填写直接覆盖的 criterion_ids。"
            "验收环境允许访问公网；run_command 可执行任意 npm 子命令以及 grep/rg。"
            "依赖安装或文本搜索成功不算独立验证，之后仍须运行测试、类型检查、构建、lint 或断言。"
            "禁止修改业务文件、访问凭据、.git 或平台服务。每个 passed 项必须引用平台返回的"
            " evidence_id；must 项未通过、命令失败或工作区被改变时 approved 必须为 false。"
            "证据充分后调用 finish_acceptance；regression_results 与 "
            "environment 由平台根据真实证据补齐，无需在工具参数中提交。"
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
        "minItems": 1,
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
            "读取初始干净 checkout 中的文件并生成验收证据。",
            {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "minLength": 1},
                    "criterion_ids": criterion_ids,
                },
                "required": ["path", "criterion_ids"],
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
                "required": ["argv", "criterion_ids"],
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
    if not isinstance(raw, list) or not raw:
        raise ValueError("read_file/run_command 必须填写非空 criterion_ids")
    values = [str(item) for item in raw]
    invalid = sorted(set(values) - set(expected_ids))
    if invalid:
        raise ValueError("criterion_ids 包含未知 id: " + ", ".join(invalid))
    return sorted(set(values))


def _tool_signature(name: str, arguments: dict[str, Any]) -> str:
    if name == "run_command":
        target = " ".join(str(item) for item in arguments.get("argv", []))
    else:
        target = str(arguments.get("path") or "")
    return f"{name}({target})"[:500]


def _validation_message(exc: ValidationError) -> str:
    issues = []
    for item in exc.errors(include_url=False, include_input=False)[:8]:
        location = ".".join(str(part) for part in item.get("loc", ())) or "$"
        issues.append(f"{location}: {item.get('msg', 'invalid value')}")
    return "AcceptanceReport 校验失败: " + "; ".join(issues)


__all__ = ["PiAcceptanceToolLoop"]
