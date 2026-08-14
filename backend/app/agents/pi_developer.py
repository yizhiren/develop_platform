from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from ..schemas.domain import DevelopmentReport
from .coding import (
    DeveloperAction,
    DeveloperToolLoop,
    _action_signature,
    _broad_rewrite_is_authorized,
    _changed_repository_ids,
    _compact_context,
    _confirmed_validation_action,
    _is_print_only_python_command,
    _is_test_execution_command,
    _is_test_file_path,
    _is_validation_command,
    _mandatory_review_changes,
    _markdown_quality_issues,
    _normalize_action,
    _python_command_mutates_workspace,
    _recoverable_missing_previous_paths,
    _required_test_review_changes,
    _restore_review_corrupted_files,
)
from .pi_bridge import PiAgentCoreBridge, PiBridgeError, PiToolDefinition, PiToolResult
from .providers import ModelResponse, OpenAICompatibleProvider
from .runtime import AgentOutputError
from .sandbox import SandboxViolation, WorkspaceSandbox


MUTATION_TOOLS = {
    "write_file",
    "replace_text",
    "replace_lines",
    "delete_file",
    "restore_file",
    "restore_previous_file",
}
READ_TOOLS = {"list_files", "search_text", "read_file", "read_lines"}


class PiDeveloperToolLoop:
    """Persistent Pi Agent3 loop backed by the existing Python sandbox gates."""

    def __init__(
        self,
        provider: OpenAICompatibleProvider,
        bridge: PiAgentCoreBridge,
        max_turns: int = 32,
    ) -> None:
        self.provider = provider
        self.bridge = bridge
        self.max_turns = min(max(max_turns, 8), 50)

    async def run(self, context: dict[str, Any]) -> tuple[DevelopmentReport, ModelResponse]:
        manifest = context.get("artifacts", {}).get("workspace_manifest")
        if not isinstance(manifest, dict) or not manifest.get("workspace_root"):
            raise AgentOutputError("developer workspace manifest is missing")
        sandbox = WorkspaceSandbox(Path(str(manifest["workspace_root"])))
        safe_context = _compact_context(context, manifest)
        automatic_recoveries, automatic_recovery_errors = _restore_review_corrupted_files(
            safe_context,
            manifest,
            sandbox,
        )
        safe_context["workspace"] = {
            "files": sandbox.list_files(limit=300),
            "automatic_recoveries": automatic_recoveries,
            "automatic_recovery_errors": automatic_recovery_errors,
            "recoverable_missing_previous_paths": _recoverable_missing_previous_paths(
                safe_context,
                sandbox,
            ),
        }
        mutated_paths: set[str] = {
            path
            for path in safe_context.get("previous_attempt_changed_paths", [])
            if isinstance(path, str)
            and _changed_repository_ids({path}, manifest.get("repositories", []))
        }
        mutated_paths.update(automatic_recoveries)
        read_paths: set[str] = set()
        successful_tests: list[dict[str, Any]] = []
        required_test_changes = _required_test_review_changes(safe_context)
        action_counts: dict[str, int] = {}
        path_read_counts: dict[str, int] = {}
        exploration_actions = 0
        final_report: DevelopmentReport | None = None

        async def handle_tool(name: str, arguments: dict[str, Any]) -> PiToolResult:
            nonlocal exploration_actions, final_report
            action_name = "finish" if name == "finish_development" else name
            try:
                action = DeveloperAction.model_validate({"action": action_name, **arguments})
            except ValidationError as exc:
                raise ValueError(_validation_message(exc, "DeveloperAction")) from exc
            action = _normalize_action(action, manifest)
            signature = _action_signature(action)
            action_counts[signature] = action_counts.get(signature, 0) + 1
            if action.action in READ_TOOLS:
                exploration_actions += 1
                if action_counts[signature] > 2:
                    raise ValueError("重复只读操作已被平台阻止；请使用已有证据进行修改或验证")
                if exploration_actions > 12:
                    raise ValueError("只读调查预算已耗尽；请完成最小修改、验证并提交报告")
            if action.action in {"read_file", "read_lines"} and action.path:
                path_read_counts[action.path] = path_read_counts.get(action.path, 0) + 1
                if path_read_counts[action.path] > 6:
                    raise ValueError("同一文件读取次数已达上限；请使用已有证据")
            if action.action == "run_command":
                argv = action.argv or []
                if _is_print_only_python_command(argv):
                    raise ValueError("禁止用 Python print/open/read_text 代替文件读取")
                if _python_command_mutates_workspace(argv):
                    raise ValueError("run_command 只用于验证，不能修改工作区文件")
                if not _is_validation_command(argv):
                    raise ValueError("run_command 必须是具有失败条件的测试、检查、lint 或构建命令")
            if action.action == "replace_lines" and action.path not in read_paths:
                raise ValueError("replace_lines 前必须读取目标文件的最新内容")

            if action.action == "finish":
                if action.report is None:
                    raise ValueError("finish_development requires a report object")
                if not successful_tests:
                    raise ValueError("finish 前必须至少运行一个成功的验证命令")
                markdown_issues = _markdown_quality_issues(sandbox, mutated_paths)
                if markdown_issues:
                    raise ValueError(
                        "Markdown 质量门禁未通过: " + "; ".join(markdown_issues[:8])
                    )
                report = action.report.model_copy(deep=True)
                report.tests = successful_tests
                report.files_changed = sorted(mutated_paths)
                changed_repositories = _changed_repository_ids(
                    mutated_paths,
                    manifest.get("repositories", []),
                )
                if required_test_changes and not any(
                    _is_test_file_path(path) for path in mutated_paths
                ):
                    raise ValueError("代码评审要求新增或修改真实测试文件")
                if required_test_changes and not any(
                    _is_test_execution_command(list(item.get("command", [])))
                    for item in successful_tests
                ):
                    raise ValueError("代码评审要求执行真实测试命令")
                validation_only_rework = (
                    not changed_repositories
                    and bool(safe_context.get("prior_commit_available"))
                    and bool(safe_context.get("artifacts", {}).get("code_review_report"))
                    and not _mandatory_review_changes(safe_context)
                )
                if not changed_repositories and not validation_only_rework:
                    raise ValueError("未检测到实际文件修改，不能 finish")
                if set(report.repositories_changed) != set(changed_repositories):
                    raise ValueError(
                        "report.repositories_changed 必须与实际修改一致；expected="
                        + json.dumps(changed_repositories, ensure_ascii=False)
                    )
                final_report = report
                return PiToolResult(
                    observation={
                        "type": "development_submitted",
                        "files_changed": sorted(mutated_paths),
                        "successful_test_count": len(successful_tests),
                    },
                    terminate=True,
                )

            allow_broad_rewrite = _broad_rewrite_is_authorized(
                action,
                read_paths,
                safe_context,
                manifest,
            )
            try:
                observation = await asyncio.to_thread(
                    DeveloperToolLoop._execute,
                    action,
                    sandbox,
                    allow_broad_rewrite=allow_broad_rewrite,
                )
            except (SandboxViolation, OSError, ValueError) as exc:
                raise ValueError(str(exc)) from exc

            validation_action = action if action.action == "run_command" else None
            validation_observation = observation
            if observation.get("controlled_broad_rewrite"):
                automatic_validation = _confirmed_validation_action(
                    safe_context,
                    manifest,
                    action.path or "",
                )
                if automatic_validation is not None:
                    automatic_observation = await asyncio.to_thread(
                        DeveloperToolLoop._execute,
                        automatic_validation,
                        sandbox,
                    )
                    observation = {
                        "type": "mutation_with_validation",
                        "mutation": observation,
                        "automatic_validation": automatic_observation,
                    }
                    validation_action = automatic_validation
                    validation_observation = automatic_observation

            if (
                action.action in {"read_file", "read_lines"}
                and action.path
                and observation.get("type") in {"file", "file_lines"}
            ):
                read_paths.add(action.path)
            if (
                action.action in MUTATION_TOOLS
                and action.path
                and observation.get("type") != "tool_error"
                and observation.get("changed", True)
            ):
                if action.action == "restore_file":
                    mutated_paths.discard(action.path)
                else:
                    mutated_paths.add(action.path)
            if validation_action is not None and validation_observation.get("type") == "command":
                if validation_observation.get("returncode") == 0:
                    successful_tests.append(
                        {
                            "command": validation_action.argv,
                            "cwd": validation_action.cwd,
                            "status": "passed",
                            "output": str(validation_observation.get("output", ""))[:4_000],
                        }
                    )
            return PiToolResult(observation=_truncate(observation))

        system_prompt = (
            "你是开发工程师（Agent3），通过 Pi Agent Core 在隔离工作区中完成已确认方案。"
            "先用 list_files/search_text/read_file/read_lines 查看真实声明、调用点与测试，再做最小"
            "充分修改。所有写入只能通过显式文件工具；run_command 只用于测试、类型检查、lint 或"
            "构建，不能修改源码。禁止访问网络、凭据、.git 或平台服务，禁止执行依赖安装、Git、"
            "shell 或任意脚本下载。接口必须以仓库真实定义和现有调用点为准，不得虚构。"
            "至少一个相关验证命令成功后才能调用 finish_development；报告中的仓库和文件必须与"
            "平台记录的实际修改完全一致。普通文本不会成为最终结果。"
            f"\nDevelopmentReport JSON Schema:\n"
            f"{json.dumps(DevelopmentReport.model_json_schema(), ensure_ascii=False)}"
        )
        try:
            response = await self.bridge.run(
                provider=self.provider,
                system_prompt=system_prompt,
                user_prompt=json.dumps(safe_context, ensure_ascii=False),
                tools=_developer_tools(),
                terminal_tools={"finish_development"},
                handler=handle_tool,
                max_turns=self.max_turns,
            )
        except PiBridgeError as exc:
            exc.changed_paths = sorted(mutated_paths)
            if mutated_paths:
                exc.retryable = True
            raise
        if final_report is None:
            raise AgentOutputError("Pi developer stopped without a valid development report")
        response.content = final_report.model_dump_json()
        return final_report, response


def _developer_tools() -> list[PiToolDefinition]:
    path = {"type": "string", "minLength": 1}
    report_schema = DevelopmentReport.model_json_schema()
    definitions = report_schema.pop("$defs", {})
    finish_parameters: dict[str, Any] = {
        "type": "object",
        "properties": {"report": report_schema},
        "required": ["report"],
        "additionalProperties": False,
    }
    if definitions:
        finish_parameters["$defs"] = definitions
    return [
        _tool("list_files", "列出文件", "列出工作区文件。", {"path": {"type": "string"}}),
        _tool(
            "search_text",
            "搜索文本",
            "按字面量搜索仓库文本。",
            {"query": {"type": "string", "minLength": 1}, "path": {"type": "string"}},
            ["query"],
        ),
        _tool("read_file", "读取文件", "读取完整的有界文本文件。", {"path": path}, ["path"]),
        _tool(
            "read_lines",
            "读取行段",
            "读取文件中的指定行段。",
            {
                "path": path,
                "start_line": {"type": "integer", "minimum": 1},
                "end_line": {"type": "integer", "minimum": 1},
            },
            ["path"],
        ),
        _tool(
            "write_file",
            "写入完整文件",
            "创建文件；已有大文件仅在已确认方案明确授权时可整文件写入。",
            {
                "path": path,
                "content": {"type": "string"},
                "rewrite_reason": {"type": "string", "maxLength": 2000},
            },
            ["path", "content"],
        ),
        _tool(
            "replace_text",
            "聚焦替换文本",
            "old 必须在最新文件中恰好出现一次。",
            {"path": path, "old": {"type": "string"}, "new": {"type": "string"}},
            ["path", "old", "new"],
        ),
        _tool(
            "replace_lines",
            "按行替换",
            "仅可替换已读取文件中不超过 160 行的范围。",
            {
                "path": path,
                "start_line": {"type": "integer", "minimum": 1},
                "end_line": {"type": "integer", "minimum": 1},
                "content": {"type": "string"},
            },
            ["path", "start_line", "end_line", "content"],
        ),
        _tool("delete_file", "删除单个文件", "删除明确目标文件。", {"path": path}, ["path"]),
        _tool(
            "restore_file",
            "恢复当前提交文件",
            "从 HEAD 恢复一个已跟踪文件。",
            {"path": path},
            ["path"],
        ),
        _tool(
            "restore_previous_file",
            "恢复父提交文件",
            "仅在评审证明当前提交损坏时从 HEAD^ 恢复单个文件。",
            {"path": path},
            ["path"],
        ),
        _tool(
            "run_command",
            "运行验证命令",
            "运行受平台白名单和无网络沙箱约束的测试或静态检查。",
            {
                "argv": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 1,
                },
                "cwd": {"type": "string", "default": "."},
            },
            ["argv"],
        ),
        PiToolDefinition(
            "finish_development",
            "提交开发报告",
            "修改和验证完成后提交完整 DevelopmentReport。",
            finish_parameters,
        ),
    ]


def _tool(
    name: str,
    label: str,
    description: str,
    properties: dict[str, Any],
    required: list[str] | None = None,
) -> PiToolDefinition:
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "additionalProperties": False,
    }
    if required:
        parameters["required"] = required
    return PiToolDefinition(name, label, description, parameters)


def _validation_message(exc: ValidationError, label: str) -> str:
    issues = []
    for item in exc.errors(include_url=False, include_input=False)[:8]:
        location = ".".join(str(part) for part in item.get("loc", ())) or "$"
        issues.append(f"{location}: {item.get('msg', 'invalid value')}")
    return f"{label} 校验失败: " + "; ".join(issues)


def _truncate(value: dict[str, Any], limit: int = 8_000) -> dict[str, Any]:
    encoded = json.dumps(value, ensure_ascii=False)
    if len(encoded) <= limit:
        return value
    return {
        "type": value.get("type", "observation"),
        "truncated": True,
        "preview": encoded[:limit],
    }


__all__ = ["PiDeveloperToolLoop"]
