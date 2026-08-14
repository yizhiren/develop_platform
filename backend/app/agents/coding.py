from __future__ import annotations

import ast
import difflib
import hashlib
import json
import re
import shlex
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError

from ..schemas.domain import DevelopmentReport
from .providers import LLMProvider, ModelResponse
from .runtime import AgentOutputError
from .sandbox import SandboxViolation, WorkspaceSandbox


class DeveloperStepBudgetExceeded(AgentOutputError):
    code = "agent.step_budget_exhausted"
    retryable = False

    def __init__(
        self,
        message: str,
        token_usage: int,
        retryable: bool = False,
        changed_paths: list[str] | None = None,
    ):
        super().__init__(message)
        self.token_usage = token_usage
        self.retryable = retryable
        self.changed_paths = list(changed_paths or [])


class DeveloperInvalidAction(AgentOutputError):
    code = "agent.invalid_action"
    retryable = False

    def __init__(self, invalid_output_count: int, token_usage: int):
        super().__init__(
            "developer model repeatedly returned output that does not match the tool action schema; "
            f"invalid_outputs={invalid_output_count}"
        )
        self.token_usage = token_usage


class DeveloperToolStalled(AgentOutputError):
    code = "agent.tool_stalled"
    retryable = False

    def __init__(self, action: str, repeated_count: int, total_count: int, detail: str, token_usage: int):
        super().__init__(
            "developer repeatedly requested tool actions that the platform could not execute; "
            f"action={action}, repeated_errors={repeated_count}, total_tool_errors={total_count}, "
            f"latest_error={_diagnostic_excerpt(detail)}"
        )
        self.token_usage = token_usage


class DeveloperValidationStalled(AgentOutputError):
    code = "agent.validation_stalled"
    retryable = False

    def __init__(
        self,
        command: str,
        repeated_count: int,
        failed_count: int,
        output: str,
        token_usage: int,
    ):
        super().__init__(
            "developer validation stopped making progress; "
            f"command={command}, repeated_failure={repeated_count}, failed_validations={failed_count}, "
            f"latest_output={_diagnostic_excerpt(output)}"
        )
        self.token_usage = token_usage


class DeveloperAction(BaseModel):
    action: Literal[
        "list_files",
        "search_text",
        "read_file",
        "read_lines",
        "write_file",
        "replace_text",
        "replace_lines",
        "delete_file",
        "restore_file",
        "restore_previous_file",
        "run_command",
        "finish",
    ]
    path: str | None = None
    query: str | None = None
    content: str | None = None
    old: str | None = None
    new: str | None = None
    start_line: int | None = Field(default=None, ge=1)
    end_line: int | None = Field(default=None, ge=1)
    rewrite_reason: str | None = Field(default=None, max_length=2000)
    argv: list[str] | None = None
    cwd: str = "."
    report: DevelopmentReport | None = None


class DeveloperToolLoop:
    FINALIZATION_GRACE_STEPS = 2
    MAX_EXPLORATION_ACTIONS = 12
    MAX_IDENTICAL_READ_ACTIONS = 2
    MAX_READ_ACTIONS_PER_PATH = 6
    MAX_TOOL_ERRORS = 10
    MAX_IDENTICAL_TOOL_ERRORS = 3
    MAX_FAILED_VALIDATIONS = 6
    MAX_IDENTICAL_VALIDATION_FAILURES = 3
    LARGE_EXISTING_FILE_BYTES = 8_000
    LARGE_REPLACEMENT_BYTES = 2_000
    MIN_REWRITE_RATIO = 0.8
    MIN_WHOLE_FILE_SIMILARITY = 0.55
    MIN_LARGE_REPLACEMENT_SIMILARITY = 0.50
    MAX_LINE_REPLACEMENT_LINES = 160

    def __init__(self, provider: LLMProvider, max_steps: int = 40):
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
            "若上下文包含 code_review_report，必须逐条落实驳回意见并补充相应测试，不能只重复上一轮报告。"
            "context.conversation 中最新的人类说明用于澄清本轮返工方式，必须遵守与需求一致的明确指导。"
            "blocker/high 的 required_change 必须真实体现在文件 Diff 中，不能只写进总结。纯打印或读取文件不算测试；"
            "Python 验证必须使用 assert、sys.exit 或显式抛错表达失败条件。commit/push 由可信 Git Worker 在 finish 后执行，绝不能提前声称已完成。"
            "run_command 仅允许 pytest、python、python3、npm、pnpm、yarn、go、cargo、make，以及仅用于内置测试运行器的 node --test；"
            "方案中的 ls/find/grep/git/test 等命令不可直接执行，"
            "应使用 list_files/search_text/read_file 查找和读取仓库事实，或用 python/python3 编写带失败条件的只读验证。"
            "run_command 只用于验证，禁止借助 Python、测试脚本或构建命令创建、修改、移动或删除源码与测试；所有交付文件修改必须使用显式文件工具。"
            "不要反复读取或搜索同一内容；确认目标后尽早完成最小充分修改。"
            "首次开发若现有实现已经基本满足需求，也必须根据验收标准做一项可审查的最小有效改进；"
            "返工时若已有 development_commit_manifest，且 code_review_report 仅要求补充验证证据，允许不修改源码，"
            "但必须运行对应的可失败验证命令，并在 finish.report 中填写 repositories_changed=[]；"
            "任何导出符号、类型、字段、配置键或调用约定都必须以工作区内的真实声明和现有调用点为依据；"
            "在使用、替换或新增接口前，先读取其声明、导出位置和至少一个当前调用点，禁止凭名称猜测或虚构 API。"
            "仓库事实与方案假设冲突时，以真实代码为准并选择现有公共边界，不得恢复已删除接口或创建伪兼容层。"
            "不得用硬编码结果、空实现、手工循环或只会通过表面断言的 mock 替代需求要求的真实生产路径；mock 只能隔离外部边界。"
            "保持最小修改面；优先局部 replace_text，禁止为了修复局部问题重写大文件或顺带生成无关产物。"
            "如果已确认架构或评审明确点名要求迁移某个现有大文件，且聚焦替换无法安全完成，可以先读取该文件，"
            "再用 write_file 提交完整内容，并在 rewrite_reason 中说明与已确认方案的对应关系；平台只会对方案明确点名且本轮已读取的文件放行。"
            "先运行聚焦的 no-emit、check、typecheck 或单测，再运行会生成产物的 build/package；不得把生成文件当作源码修复。"
            "文档整理任务优先补齐明确的文件清单、架构章节或验证说明，不得用无限调查代替交付。"
            "修改文档前必须检查目标章节是否已存在；已存在的内容不得为了回应 finding 再复制一份。"
            "每次成功读取后，平台会把可复用内容保存在 read_evidence；后续步骤必须优先使用该证据，禁止为了找回上下文反复读取同一文件。"
            "仅要求补充扫描、Diff 或测试证据的 finding 应通过可失败的验证命令和 finish 报告回应，不得伪造重复内容。"
            "命令失败后必须根据 observation 的退出码和输出修正或简化，禁止反复运行等价的失败脚本。"
            "静态检查报告未知符号、字段或参数不匹配时，下一步必须读取对应真实定义和工作调用点；"
            "相关代码没有改变、预期错误没有被逐项处理前，禁止重跑同一验证命令。"
            "replace_text 的 old 必须在文件中恰好出现一次；若无法构造唯一片段，先用 read_lines 获取最新行号，"
            "再用 replace_lines 做不超过 160 行的聚焦替换。replace_lines 只能用于本轮已经读取过的文件，禁止猜测行号。"
            "npx 仅支持仓库本地已有的 tsc、eslint、vitest、jest、prettier 校验工具，平台会强制 --no-install，禁止下载或执行任意包。"
            "写入仓库的文档只能使用仓库相对路径，不得记录 workspace_root、repository_id、临时目录、容器路径或 UUID；"
            "例如应写 AGENTS.md，而不是 <repository_id>/AGENTS.md。"
            "若上一次尝试误删或破坏了已跟踪文件，且最新验证明确因该文件缺失而失败，可使用 restore_file 将该单个文件恢复到当前 HEAD；"
            "恢复后必须重新读取、做必要的聚焦修改并验证，禁止把 restore_file 当作仓库级回退。"
            "若 code_review_report 明确证明当前 HEAD 中的某个已提交文件被截断、覆盖或严重破坏，"
            "必须先用 restore_previous_file 从 HEAD 的父提交恢复该单个文件，再读取、做聚焦修改并运行真实测试；禁止凭空重写原文件。"
            "使用相对 workspace_root 的路径；多个仓库位于各自 repository_id 目录。finish.report 必须准确描述实际改动和测试。"
            f"\nDeveloperAction JSON Schema:\n{action_schema}"
        )
        safe_context = _compact_context(context, manifest)
        automatic_recoveries, automatic_recovery_errors = _restore_review_corrupted_files(
            safe_context,
            manifest,
            sandbox,
        )
        observation: dict[str, Any] = {
            "type": "workspace",
            "files": sandbox.list_files(limit=300),
            "automatic_recoveries": automatic_recoveries,
            "automatic_recovery_errors": automatic_recovery_errors,
        }
        transcript: list[dict[str, Any]] = []
        prompt_tokens = completion_tokens = 0
        model = ""
        successful_tests: list[dict[str, Any]] = []
        required_test_changes = _required_test_review_changes(safe_context)
        missing_previous_paths = _recoverable_missing_previous_paths(safe_context, sandbox)
        mutated_paths: set[str] = {
            path
            for path in safe_context.get("previous_attempt_changed_paths", [])
            if isinstance(path, str) and _changed_repository_ids({path}, manifest.get("repositories", []))
        }
        mutated_paths.update(automatic_recoveries)
        action_counts: dict[str, int] = {}
        read_path_counts: dict[str, int] = {}
        invalid_output_count = 0
        exploration_actions = 0
        blocked_exploration_actions = 0
        tool_error_count = 0
        tool_error_counts: dict[str, int] = {}
        failed_validation_count = 0
        validation_failure_counts: dict[str, int] = {}
        latest_validation_failure: dict[str, Any] = {}
        read_paths: set[str] = set()
        read_evidence: dict[str, dict[str, Any]] = {}
        for step in range(1, self.max_steps + self.FINALIZATION_GRACE_STEPS + 1):
            finalization_only = step > self.max_steps
            if finalization_only and not (mutated_paths and successful_tests):
                break
            steps_remaining = max(self.max_steps - step + 1, 0)
            user_payload = {
                "context": safe_context,
                "recent_transcript": transcript[-6:],
                "observation": observation,
                "step": step,
                "steps_remaining": steps_remaining,
                "progress": {
                    "mutated_paths": sorted(mutated_paths),
                    "successful_test_count": len(successful_tests),
                    "exploration_actions": exploration_actions,
                    "exploration_limit": self.MAX_EXPLORATION_ACTIONS,
                    "tool_error_count": tool_error_count,
                    "tool_error_limit": self.MAX_TOOL_ERRORS,
                    "failed_validation_count": failed_validation_count,
                    "failed_validation_limit": self.MAX_FAILED_VALIDATIONS,
                    "latest_validation_failure": latest_validation_failure,
                    "mandatory_review_changes": _mandatory_review_changes(safe_context),
                    "required_test_changes": required_test_changes,
                    "required_test_file_present": any(
                        _is_test_file_path(path) for path in mutated_paths
                    ),
                    "required_test_command_passed": any(
                        _is_test_execution_command(list(item.get("command", [])))
                        for item in successful_tests
                        if isinstance(item, dict)
                    ),
                    "recoverable_missing_previous_paths": missing_previous_paths,
                },
                "read_evidence": read_evidence,
                "completion_instruction": (
                    "已有成功测试。若没有尚未执行的必要修改，立即返回 finish，并填写准确的 report；不要重复读取或测试。"
                    if successful_tests
                    else "继续检查、修改并运行相关测试；尚不能 finish。"
                ),
            }
            if exploration_actions >= self.MAX_EXPLORATION_ACTIONS - 2:
                user_payload["completion_instruction"] = (
                    "只读调查预算即将用尽。禁止重复读取已经看过的文件；使用最近一次 read_file/read_lines 的内容立即完成聚焦修改，"
                    "随后运行最窄验证并 finish。"
                    if mutated_paths
                    else "只读调查预算即将用尽。你已经拥有足够上下文；下一步必须完成一项符合验收标准的最小有效修改，随后验证并 finish。"
                )
            elif steps_remaining <= 8 and not mutated_paths:
                user_payload["completion_instruction"] = (
                    "尚未产生任何文件修改，且步骤即将耗尽。停止扩展调查：立即定位需求直接涉及的文件，完成最小充分修改，"
                    "然后用允许的命令验证并 finish。若是文档任务，可用 python -c 做存在性、章节和敏感信息检查。"
                )
            elif steps_remaining <= 4 and mutated_paths and not successful_tests:
                user_payload["completion_instruction"] = (
                    "文件已经修改但尚无成功验证。下一步必须运行一个聚焦且允许的验证命令；验证成功后立即 finish。"
                )
            elif steps_remaining <= 2 and successful_tests:
                user_payload["completion_instruction"] = (
                    "步骤即将耗尽。下一响应必须是 finish，report.repositories_changed 必须填写实际修改的 repository_id。"
                )
            if tool_error_count >= 2:
                user_payload["completion_instruction"] = (
                    "工具调用已经多次被平台拒绝。不要重试相同动作；严格使用 DeveloperAction Schema、允许的命令和仓库相对路径，"
                    "改用可执行的最小动作继续。再次重复同类错误会终止任务。"
                )
            if failed_validation_count >= 3:
                user_payload["completion_instruction"] = (
                    "验证已连续失败多次。停止扩大修改范围，也不要直接重跑。先根据 latest_validation_failure 逐项定位真实声明和现有调用点，"
                    "只做一个能消除已知错误的聚焦修改，再运行最窄验证；继续重复相同失败将终止任务。"
                )
            missing_required_test_file = required_test_changes and not any(
                _is_test_file_path(path) for path in mutated_paths
            )
            missing_required_test_command = required_test_changes and not any(
                _is_test_execution_command(list(item.get("command", [])))
                for item in successful_tests
                if isinstance(item, dict)
            )
            if missing_required_test_file or missing_required_test_command:
                missing_parts = []
                if missing_required_test_file:
                    missing_parts.append("创建或修改真实测试文件并产生可审查 Diff")
                if missing_required_test_command:
                    missing_parts.append("运行真实测试命令并通过")
                user_payload["completion_instruction"] = (
                    "代码审查明确要求测试，当前门禁仍缺少："
                    + "、".join(missing_parts)
                    + "。下一步必须优先补齐这些缺口；在两项都满足前禁止 finish。"
                    "测试必须执行生产行为并断言结果，不能用源码字符串计数、打印或只运行 typecheck/build 代替。"
                )
            if missing_previous_paths:
                user_payload["completion_instruction"] = (
                    "上一次尝试留下了缺失文件，且最近验证错误明确引用了这些路径："
                    + "、".join(missing_previous_paths)
                    + "。下一步先对相应路径调用 restore_file，随后读取恢复内容、重新应用必要的聚焦修改并验证；禁止继续读取不存在的文件或重复失败命令。"
                )
            if finalization_only:
                user_payload["completion_instruction"] = (
                    "正常工具步骤已经用尽，但已有文件修改和成功测试。当前是只允许提交报告的收尾槽："
                    "本响应必须返回 finish，并准确填写 report.repositories_changed、summary、tests 和 unresolved_risks；"
                    "任何其他 action 都会被拒绝。"
                )
            response = await self.provider.complete(system, json.dumps(user_payload, ensure_ascii=False))
            prompt_tokens += response.prompt_tokens
            completion_tokens += response.completion_tokens
            model = response.model or model
            try:
                action = DeveloperAction.model_validate_json(response.content)
            except ValidationError as exc:
                invalid_output_count += 1
                observation = {"type": "error", "message": "输出不符合 DeveloperAction JSON Schema"}
                transcript.append({"action": "invalid_json"})
                if step == self.max_steps:
                    raise DeveloperInvalidAction(
                        invalid_output_count,
                        prompt_tokens + completion_tokens,
                    ) from exc
                continue
            action = _normalize_action(action, manifest)
            signature = _action_signature(action)
            if finalization_only and action.action != "finish":
                observation = {
                    "type": "action_blocked",
                    "action": signature,
                    "message": "收尾槽只允许 finish；不得再读取、修改或运行命令",
                }
                transcript.append({"action": signature, "observation": observation})
                continue
            action_counts[signature] = action_counts.get(signature, 0) + 1
            read_only_action = action.action in {"list_files", "search_text", "read_file", "read_lines"}
            duplicate_read = read_only_action and action_counts[signature] > self.MAX_IDENTICAL_READ_ACTIONS
            read_path = action.path if action.action in {"read_file", "read_lines"} else None
            if read_path:
                read_path_counts[read_path] = read_path_counts.get(read_path, 0) + 1
            repeated_path_read = bool(
                read_path and read_path_counts[read_path] > self.MAX_READ_ACTIONS_PER_PATH
            )
            exploration_exhausted = read_only_action and exploration_actions >= self.MAX_EXPLORATION_ACTIONS
            if duplicate_read or repeated_path_read or exploration_exhausted:
                blocked_exploration_actions += 1
                observation = {
                    "type": "action_blocked",
                    "action": signature,
                    "message": (
                        "重复只读操作已被平台阻止。使用最近一次读取结果执行聚焦修改或验证，不要再读取同一内容。"
                        if duplicate_read or repeated_path_read
                        else "只读调查预算已耗尽。使用已经获得的证据执行聚焦修改或验证，不要再扩大调查。"
                    ),
                    "blocked_count": blocked_exploration_actions,
                }
            else:
                if read_only_action:
                    exploration_actions += 1
                if _is_print_only_python_command(action.argv or []) if action.action == "run_command" else False:
                    observation = {
                        "type": "action_blocked",
                        "action": action.action,
                        "message": "禁止用 Python print/open/read_text 代替文件读取；请使用 read_file 或 read_lines，并把步骤留给实现与验证。",
                    }
                elif action.action == "run_command" and _python_command_mutates_workspace(action.argv or []):
                    observation = {
                        "type": "action_blocked",
                        "action": action.action,
                        "message": (
                            "run_command 仅用于验证，不能创建、修改、移动或删除工作区文件；"
                            "请用 write_file、replace_text、replace_lines 或 delete_file 等显式文件工具提交可追踪修改。"
                        ),
                    }
                elif action.action == "replace_lines" and action.path not in read_paths:
                    observation = {
                        "type": "tool_error",
                        "action": action.action,
                        "message": "replace_lines requires reading the latest target file first",
                    }
                else:
                    try:
                        observation = self._execute(
                            action,
                            sandbox,
                            allow_broad_rewrite=_broad_rewrite_is_authorized(
                                action,
                                read_paths,
                                safe_context,
                                manifest,
                            ),
                        )
                    except (SandboxViolation, OSError, ValueError) as exc:
                        observation = {"type": "tool_error", "action": action.action, "message": str(exc)}
            validation_action = action if action.action == "run_command" else None
            validation_observation = observation
            if observation.get("controlled_broad_rewrite"):
                automatic_validation = _confirmed_validation_action(
                    safe_context,
                    manifest,
                    action.path or "",
                )
                if automatic_validation is not None:
                    try:
                        automatic_observation = self._execute(automatic_validation, sandbox)
                    except (SandboxViolation, OSError, ValueError) as exc:
                        automatic_observation = {
                            "type": "tool_error",
                            "action": automatic_validation.action,
                            "message": str(exc),
                        }
                    observation = {
                        "type": "mutation_with_validation",
                        "mutation": observation,
                        "automatic_validation": automatic_observation,
                    }
                    validation_action = automatic_validation
                    validation_observation = automatic_observation
            transcript.append({"action": signature, "observation": _truncate(observation)})
            if (
                action.action in {"read_file", "read_lines"}
                and action.path
                and observation.get("type") in {"file", "file_lines"}
            ):
                read_paths.add(action.path)
                _remember_read_evidence(read_evidence, action.path, observation)
            if observation.get("type") == "tool_error":
                tool_error_count += 1
                tool_error_counts[signature] = tool_error_counts.get(signature, 0) + 1
                repeated_tool_errors = tool_error_counts[signature]
                if (
                    repeated_tool_errors >= self.MAX_IDENTICAL_TOOL_ERRORS
                    or tool_error_count >= self.MAX_TOOL_ERRORS
                ):
                    raise DeveloperToolStalled(
                        signature,
                        repeated_tool_errors,
                        tool_error_count,
                        str(observation.get("message", "tool error")),
                        prompt_tokens + completion_tokens,
                    )
            if (
                action.action in {
                    "write_file",
                    "replace_text",
                    "replace_lines",
                    "delete_file",
                    "restore_file",
                    "restore_previous_file",
                }
                and action.path
                and observation.get("type") != "tool_error"
                and (
                    observation.get("changed", True)
                    or (action.action == "delete_file" and safe_context.get("prior_commit_available"))
                )
            ):
                if action.action == "restore_file":
                    mutated_paths.discard(action.path)
                    missing_previous_paths = [
                        path for path in missing_previous_paths if path != action.path
                    ]
                else:
                    mutated_paths.add(action.path)
                if action.action != "delete_file":
                    try:
                        _remember_file_content(read_evidence, action.path, sandbox.read_file(action.path))
                    except SandboxViolation:
                        pass
            if validation_action is not None and validation_observation.get("type") == "command":
                validation_command = _is_validation_command(validation_action.argv or [])
                if validation_observation.get("returncode") == 0 and validation_command:
                    successful_tests.append(
                        {
                            "command": validation_action.argv,
                            "cwd": validation_action.cwd,
                            "status": "passed",
                            "output": validation_observation.get("output", "")[:4000],
                        }
                    )
                    failed_validation_count = 0
                    validation_failure_counts.clear()
                    latest_validation_failure = {}
                elif validation_observation.get("returncode") == 0:
                    validation_observation = {
                        **validation_observation,
                        "validation_warning": (
                            "命令虽然退出码为 0，但只读取或打印内容，没有断言失败条件，因此不计为成功测试。"
                        ),
                    }
                    transcript[-1] = {"action": signature, "observation": _truncate(validation_observation)}
                elif validation_command:
                    failed_validation_count += 1
                    validation_signature = _action_signature(validation_action)
                    failure_key = _validation_failure_key(
                        validation_signature,
                        str(validation_observation.get("output", "")),
                    )
                    validation_failure_counts[failure_key] = validation_failure_counts.get(failure_key, 0) + 1
                    repeated_failure = validation_failure_counts[failure_key]
                    latest_validation_failure = {
                        "command": validation_action.argv,
                        "cwd": validation_action.cwd,
                        "returncode": validation_observation.get("returncode"),
                        "output": _diagnostic_excerpt(str(validation_observation.get("output", "")), 2_000),
                        "repeated_failure": repeated_failure,
                    }
                    if (
                        repeated_failure >= self.MAX_IDENTICAL_VALIDATION_FAILURES
                        or failed_validation_count >= self.MAX_FAILED_VALIDATIONS
                    ):
                        raise DeveloperValidationStalled(
                            validation_signature,
                            repeated_failure,
                            failed_validation_count,
                            str(validation_observation.get("output", "")),
                            prompt_tokens + completion_tokens,
                        )
            if action.action == "finish":
                if action.report is None:
                    observation = {"type": "error", "message": "finish 必须包含 report"}
                    continue
                if not successful_tests:
                    observation = {"type": "error", "message": "finish 前必须至少运行一个成功的测试命令"}
                    continue
                markdown_issues = _markdown_quality_issues(sandbox, mutated_paths)
                if markdown_issues:
                    observation = {
                        "type": "error",
                        "message": "Markdown 质量门禁未通过，修复后才能 finish",
                        "issues": markdown_issues,
                    }
                    continue
                report = action.report.model_copy(deep=True)
                report.tests = successful_tests
                changed_repositories = _changed_repository_ids(mutated_paths, manifest.get("repositories", []))
                report.files_changed = sorted(mutated_paths)
                if required_test_changes and not any(
                    _is_test_file_path(path) for path in mutated_paths
                ):
                    observation = {
                        "type": "error",
                        "message": "代码审查要求新增或修改测试；必须产生真实测试文件 Diff，不能用源码字符串扫描代替",
                        "required_changes": required_test_changes,
                    }
                    continue
                if required_test_changes and not any(
                    _is_test_execution_command(list(item.get("command", [])))
                    for item in successful_tests
                    if isinstance(item, dict)
                ):
                    observation = {
                        "type": "error",
                        "message": "代码审查要求执行测试；必须运行 pytest、npm test、vitest 或等价测试命令",
                        "required_changes": required_test_changes,
                    }
                    continue
                validation_only_rework = (
                    not changed_repositories
                    and bool(safe_context.get("prior_commit_available"))
                    and bool(safe_context.get("artifacts", {}).get("code_review_report"))
                    and not _mandatory_review_changes(safe_context)
                )
                if not changed_repositories and not validation_only_rework:
                    observation = {"type": "error", "message": "未检测到实际文件修改，不能 finish"}
                    continue
                if set(report.repositories_changed) != set(changed_repositories):
                    observation = {
                        "type": "error",
                        "message": "report.repositories_changed 必须与实际修改一致",
                        "expected_repository_ids": changed_repositories,
                    }
                    continue
                return report, ModelResponse(
                    content=report.model_dump_json(),
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    model=model,
                )
        action_trace = ", ".join(str(item.get("action", "unknown")) for item in transcript[-10:])
        validation_detail = ""
        if latest_validation_failure:
            validation_detail = (
                "; latest_validation_failure="
                + _diagnostic_excerpt(json.dumps(latest_validation_failure, ensure_ascii=False), 2_000)
            )
        raise DeveloperStepBudgetExceeded(
            "developer tool loop exhausted its step budget; "
            f"file_changes={len(mutated_paths)}, successful_tests={len(successful_tests)}, "
            f"{validation_detail.lstrip('; ')}; "
            f"recent_actions=[{action_trace}]",
            prompt_tokens + completion_tokens,
            retryable=bool(mutated_paths),
            changed_paths=sorted(mutated_paths),
        )

    @staticmethod
    def _execute(
        action: DeveloperAction,
        sandbox: WorkspaceSandbox,
        *,
        allow_broad_rewrite: bool = False,
    ) -> dict[str, Any]:
        if action.action == "list_files":
            return {"type": "files", "files": sandbox.list_files(action.path or ".", limit=300)}
        if action.action == "search_text":
            if not action.query:
                raise ValueError("search_text requires query")
            return {
                "type": "search_results",
                "query": action.query,
                "matches": sandbox.search_text(action.query, action.path or ".", limit=100),
            }
        if action.action == "read_file":
            if not action.path:
                raise ValueError("read_file requires path")
            return {"type": "file", "path": action.path, "content": sandbox.read_file(action.path)[:80_000]}
        if action.action == "read_lines":
            if not action.path:
                raise ValueError("read_lines requires path")
            content = sandbox.read_file(action.path)
            lines = content.splitlines()
            start_line = action.start_line or 1
            if start_line > max(len(lines), 1):
                raise ValueError(f"read_lines start_line exceeds file length ({len(lines)})")
            end_line = min(action.end_line or (start_line + 199), len(lines))
            if end_line < start_line:
                raise ValueError("read_lines end_line must be greater than or equal to start_line")
            return {
                "type": "file_lines",
                "path": action.path,
                "start_line": start_line,
                "end_line": end_line,
                "total_lines": len(lines),
                "lines": [
                    {"line": index, "text": lines[index - 1]}
                    for index in range(start_line, end_line + 1)
                ],
            }
        if action.action == "write_file":
            if not action.path or action.content is None:
                raise ValueError("write_file requires path and content")
            try:
                existing = sandbox.read_file(action.path)
            except SandboxViolation:
                existing = ""
            if (
                not allow_broad_rewrite
                and
                len(existing.encode()) >= DeveloperToolLoop.LARGE_EXISTING_FILE_BYTES
                and len(action.content.encode()) < len(existing.encode()) * DeveloperToolLoop.MIN_REWRITE_RATIO
            ):
                raise ValueError(
                    "refusing a destructive whole-file rewrite that removes more than 20% of a large existing file; "
                    "use replace_text for a focused edit that preserves existing content, or after reading a file explicitly named "
                    "by the confirmed architecture/review retry write_file with rewrite_reason"
                )
            if (
                not allow_broad_rewrite
                and
                len(existing.encode()) >= DeveloperToolLoop.LARGE_EXISTING_FILE_BYTES
                and _text_similarity(existing, action.content) < DeveloperToolLoop.MIN_WHOLE_FILE_SIMILARITY
            ):
                raise ValueError(
                    "refusing a broad whole-file rewrite of a large existing file; "
                    "read the actual declarations and use focused replace_text edits, or provide rewrite_reason when the confirmed "
                    "architecture/review explicitly names this migration target"
                )
            sandbox.write_file(action.path, action.content)
            return {
                "type": "write",
                "path": action.path,
                "bytes": len(action.content.encode()),
                "controlled_broad_rewrite": allow_broad_rewrite,
            }
        if action.action == "replace_text":
            if not action.path or action.old is None or action.new is None:
                raise ValueError("replace_text requires path, old and new")
            if (
                len(action.old.encode()) >= DeveloperToolLoop.LARGE_REPLACEMENT_BYTES
                and len(action.new.encode()) < len(action.old.encode()) * DeveloperToolLoop.MIN_REWRITE_RATIO
            ):
                raise ValueError(
                    "refusing a destructive large replacement that removes more than 20% of the selected content; "
                    "preserve the durable detail and make smaller focused edits"
                )
            if (
                len(action.old.encode()) >= DeveloperToolLoop.LARGE_REPLACEMENT_BYTES
                and _text_similarity(action.old, action.new) < DeveloperToolLoop.MIN_LARGE_REPLACEMENT_SIMILARITY
            ):
                raise ValueError(
                    "refusing a broad large replacement with insufficient preserved context; "
                    "split the change into focused edits grounded in existing declarations"
                )
            sandbox.replace_text(action.path, action.old, action.new)
            return {"type": "replace", "path": action.path}
        if action.action == "replace_lines":
            if (
                not action.path
                or action.content is None
                or action.start_line is None
                or action.end_line is None
            ):
                raise ValueError("replace_lines requires path, content, start_line and end_line")
            line_count = action.end_line - action.start_line + 1
            if line_count < 1:
                raise ValueError("replace_lines end_line must be greater than or equal to start_line")
            if line_count > DeveloperToolLoop.MAX_LINE_REPLACEMENT_LINES:
                raise ValueError(
                    f"replace_lines is limited to {DeveloperToolLoop.MAX_LINE_REPLACEMENT_LINES} lines; "
                    "split broad changes into focused edits"
                )
            sandbox.replace_lines(
                action.path,
                action.start_line,
                action.end_line,
                action.content,
            )
            return {
                "type": "replace_lines",
                "path": action.path,
                "start_line": action.start_line,
                "end_line": action.end_line,
            }
        if action.action == "delete_file":
            if not action.path:
                raise ValueError("delete_file requires path")
            changed = sandbox.delete_file(action.path)
            return {
                "type": "delete",
                "path": action.path,
                "changed": changed,
                "message": "file deleted" if changed else "file was already absent; do not retry deletion",
            }
        if action.action == "restore_file":
            if not action.path:
                raise ValueError("restore_file requires path")
            sandbox.restore_file(action.path)
            return {"type": "restore", "path": action.path}
        if action.action == "restore_previous_file":
            if not action.path:
                raise ValueError("restore_previous_file requires path")
            sandbox.restore_file(action.path, source="HEAD^")
            return {"type": "restore_previous", "path": action.path}
        if action.action == "run_command":
            if not action.argv:
                raise ValueError("run_command requires argv")
            if _python_command_mutates_workspace(action.argv):
                raise ValueError("Python validation command may not mutate workspace files")
            result = sandbox.run(action.argv, action.cwd)
            return {"type": "command", "argv": result.argv, "returncode": result.returncode, "output": result.output, "truncated": result.truncated}
        if action.action == "finish":
            return {"type": "finish"}
        raise ValueError("unsupported developer action")


def _truncate(value: dict[str, Any], limit: int = 6_000) -> dict[str, Any]:
    encoded = json.dumps(value, ensure_ascii=False)
    if len(encoded) <= limit:
        return value
    return {"type": value.get("type", "observation"), "truncated": True, "preview": encoded[:limit]}


def _diagnostic_excerpt(value: str, limit: int = 1_500) -> str:
    compact = " ".join(value.split())
    if len(compact) <= limit:
        return compact or "no diagnostic output"
    lines = value.splitlines()
    salient_markers = (
        "not ok ",
        "failuretype:",
        "error:",
        "error ts",
        "failed",
        "assertionerror",
        "traceback ",
    )
    salient_indexes = [
        index
        for index, line in enumerate(lines)
        if any(marker in line.strip().casefold() for marker in salient_markers)
    ]
    if salient_indexes:
        selected: list[str] = []
        included: set[int] = set()
        for index in salient_indexes[:6]:
            for cursor in range(max(index - 2, 0), min(index + 12, len(lines))):
                if cursor not in included:
                    selected.append(lines[cursor])
                    included.add(cursor)
        salient = " ".join("\n".join(selected).split())
        if len(salient) >= limit:
            return salient[:limit]
        remaining = limit - len(salient) - 25
        if remaining > 0:
            return f"{salient} ... [output tail] ... {compact[-remaining:]}"
        return salient
    head = limit // 2
    tail = limit - head
    return f"{compact[:head]} ... [truncated] ... {compact[-tail:]}"


def _remember_read_evidence(
    cache: dict[str, dict[str, Any]],
    path: str,
    observation: dict[str, Any],
) -> None:
    if observation.get("type") == "file":
        _remember_file_content(cache, path, str(observation.get("content", "")))
        return
    rows = observation.get("lines", [])
    excerpt = "\n".join(
        f"{item.get('line')}: {item.get('text', '')}"
        for item in rows
        if isinstance(item, dict)
    )[:10_000]
    _cache_evidence(cache, path, {
        "source": "read_lines",
        "range": [observation.get("start_line"), observation.get("end_line")],
        "total_lines": observation.get("total_lines"),
        "content": excerpt,
    })


def _remember_file_content(cache: dict[str, dict[str, Any]], path: str, content: str) -> None:
    lines = content.splitlines()
    head = "\n".join(lines[:160])[:10_000]
    declarations = "\n".join(
        f"{index}: {line}"
        for index, line in enumerate(lines, start=1)
        if re.search(r"\b(export|interface|type|class|function|const)\b", line)
    )[:6_000]
    _cache_evidence(cache, path, {
        "source": "read_file",
        "total_lines": len(lines),
        "head": head,
        "declarations": declarations,
    })


def _cache_evidence(
    cache: dict[str, dict[str, Any]],
    path: str,
    evidence: dict[str, Any],
) -> None:
    cache.pop(path, None)
    cache[path] = evidence
    while len(cache) > 5:
        cache.pop(next(iter(cache)))


def _validation_failure_key(command: str, output: str) -> str:
    normalized_output = " ".join(output.split())[:8_000]
    return hashlib.sha256(f"{command}\n{normalized_output}".encode()).hexdigest()


def _text_similarity(left: str, right: str) -> float:
    return difflib.SequenceMatcher(None, left.splitlines(), right.splitlines()).ratio()


def _action_signature(action: DeveloperAction) -> str:
    if action.action == "run_command":
        target = " ".join(action.argv or [])
    elif action.action == "search_text":
        target = f"{action.path or '.'}:{action.query or ''}"
    elif action.action == "replace_text":
        source_digest = hashlib.sha256((action.old or "").encode()).hexdigest()[:12]
        target = f"{action.path or ''}:source-{source_digest}"
    elif action.action in {"read_lines", "replace_lines"}:
        target = f"{action.path or ''}:{action.start_line or ''}-{action.end_line or ''}"
    else:
        target = action.path or action.cwd
    return f"{action.action}({target})"[:500]


def _normalize_action(action: DeveloperAction, manifest: dict[str, Any]) -> DeveloperAction:
    updates: dict[str, Any] = {}
    if action.action == "run_command" and action.argv and action.argv[0] == "-c":
        updates["argv"] = ["python", *action.argv]
    repositories = manifest.get("repositories", [])
    if len(repositories) == 1:
        repository_root = str(
            repositories[0].get("relative_path") or repositories[0].get("repository_id") or ""
        ).strip("/")
        if repository_root:
            if action.path and action.path != repository_root and not action.path.startswith(f"{repository_root}/"):
                updates["path"] = f"{repository_root}/{action.path.lstrip('/')}"
            if action.action == "run_command" and action.cwd == ".":
                updates["cwd"] = repository_root
    return action.model_copy(update=updates) if updates else action


def _restore_review_corrupted_files(
    context: dict[str, Any],
    manifest: dict[str, Any],
    sandbox: WorkspaceSandbox,
) -> tuple[list[str], list[dict[str, str]]]:
    """Deterministically undo a reviewer-proven single-file corruption from HEAD."""
    if not context.get("prior_commit_available"):
        return [], []
    review = context.get("artifacts", {}).get("code_review_report", {})
    findings = review.get("findings", []) if isinstance(review, dict) else []
    corruption_markers = (
        "不完整",
        "严重破坏",
        "截断",
        "覆盖",
        "corrupt",
        "incomplete",
        "overwrite",
        "truncat",
    )
    recovered: list[str] = []
    errors: list[dict[str, str]] = []
    for finding in findings:
        if not isinstance(finding, dict) or finding.get("severity") not in {"blocker", "high"}:
            continue
        path = str(finding.get("path") or "").strip()
        detail = " ".join(
            str(finding.get(field) or "")
            for field in ("title", "rationale", "required_change")
        ).casefold()
        if not path or not any(marker in detail for marker in corruption_markers):
            continue
        normalized = _normalize_action(
            DeveloperAction(action="restore_previous_file", path=path),
            manifest,
        ).path
        if not normalized or normalized in recovered:
            continue
        try:
            sandbox.restore_file(normalized, source="HEAD^")
            recovered.append(normalized)
        except (SandboxViolation, OSError, ValueError) as exc:
            errors.append({"path": normalized, "message": str(exc)[:500]})
    return recovered, errors


def _broad_rewrite_is_authorized(
    action: DeveloperAction,
    read_paths: set[str],
    context: dict[str, Any],
    manifest: dict[str, Any],
) -> bool:
    if action.action != "write_file" or not action.path or not action.rewrite_reason:
        return False
    if action.path not in read_paths or len(action.rewrite_reason.strip()) < 20:
        return False
    local_path = action.path
    for repository in manifest.get("repositories", []):
        repository_root = str(repository.get("relative_path") or repository.get("repository_id") or "").strip("/")
        if repository_root and local_path.startswith(f"{repository_root}/"):
            local_path = local_path[len(repository_root) + 1 :]
            break
    confirmed_artifacts = {
        "architecture": context.get("artifacts", {}).get("architecture", {}),
        "code_review_report": context.get("artifacts", {}).get("code_review_report", {}),
    }
    confirmed_text = json.dumps(confirmed_artifacts, ensure_ascii=False)
    return local_path in confirmed_text


def _confirmed_validation_action(
    context: dict[str, Any],
    manifest: dict[str, Any],
    changed_path: str,
) -> DeveloperAction | None:
    repository_id = ""
    repository_root = ""
    for repository in manifest.get("repositories", []):
        candidate_id = str(repository.get("repository_id") or "")
        candidate_root = str(repository.get("relative_path") or candidate_id).strip("/")
        if candidate_root and (
            changed_path == candidate_root or changed_path.startswith(f"{candidate_root}/")
        ):
            repository_id = candidate_id
            repository_root = candidate_root
            break
    if not repository_id or not repository_root:
        return None
    architecture_repositories = (
        context.get("artifacts", {}).get("architecture", {}).get("repositories", [])
    )
    for repository in architecture_repositories:
        if not isinstance(repository, dict) or str(repository.get("repository_id") or "") != repository_id:
            continue
        for command in repository.get("test_commands", []):
            if not isinstance(command, str):
                continue
            try:
                argv = shlex.split(command)
            except ValueError:
                continue
            if not argv or _is_dependency_mutation_command(argv) or not _is_validation_command(argv):
                continue
            return DeveloperAction(action="run_command", argv=argv, cwd=repository_root)
    return None


def _is_dependency_mutation_command(argv: list[str]) -> bool:
    if not argv:
        return False
    executable = Path(argv[0]).name
    lowered = [item.lower() for item in argv[1:3]]
    if executable == "npm":
        return any(item in {"ci", "install", "add"} for item in lowered)
    if executable in {"pnpm", "yarn"}:
        return any(item in {"install", "add"} for item in lowered)
    return False


def _is_print_only_python_command(argv: list[str]) -> bool:
    if not argv or Path(argv[0]).name not in {"python", "python3"} or "-c" not in argv:
        return False
    index = argv.index("-c")
    script = argv[index + 1] if len(argv) > index + 1 else ""
    validation_markers = ("assert ", "assert(", "sys.exit", "raise SystemExit", "raise AssertionError")
    inspection_markers = ("print(", "read_text(", "open(")
    return any(marker in script for marker in inspection_markers) and not any(
        marker in script for marker in validation_markers
    )


def _python_command_mutates_workspace(argv: list[str]) -> bool:
    """Reject inline Python that bypasses explicit, auditable file mutation tools."""
    if not argv or Path(argv[0]).name not in {"python", "python3"} or "-c" not in argv:
        return False
    index = argv.index("-c")
    script = argv[index + 1] if len(argv) > index + 1 else ""
    try:
        tree = ast.parse(script)
    except SyntaxError:
        return False

    mutating_path_methods = {
        "chmod",
        "hardlink_to",
        "lchmod",
        "mkdir",
        "rename",
        "rmdir",
        "symlink_to",
        "touch",
        "unlink",
        "write_bytes",
        "write_text",
    }
    mutating_calls = {
        "os.chmod",
        "os.link",
        "os.makedirs",
        "os.mkdir",
        "os.remove",
        "os.removedirs",
        "os.rename",
        "os.renames",
        "os.replace",
        "os.rmdir",
        "os.symlink",
        "os.truncate",
        "os.unlink",
        "shutil.copy",
        "shutil.copy2",
        "shutil.copyfile",
        "shutil.copytree",
        "shutil.move",
        "shutil.rmtree",
    }
    process_escape_calls = {
        "os.popen",
        "os.system",
        "subprocess.call",
        "subprocess.Popen",
        "subprocess.run",
    }
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        call_name = _python_call_name(node.func)
        method_name = call_name.rsplit(".", 1)[-1]
        if (
            method_name in mutating_path_methods
            or call_name in mutating_calls
            or call_name in process_escape_calls
        ):
            return True
        if method_name != "open":
            continue
        mode_node: ast.AST | None = None
        if isinstance(node.func, ast.Name) and len(node.args) >= 2:
            mode_node = node.args[1]
        elif isinstance(node.func, ast.Attribute) and node.args:
            mode_node = node.args[0]
        else:
            mode_node = next(
                (keyword.value for keyword in node.keywords if keyword.arg == "mode"),
                None,
            )
        if isinstance(mode_node, ast.Constant) and isinstance(mode_node.value, str):
            if any(marker in mode_node.value for marker in ("w", "a", "x", "+")):
                return True
    return False


def _python_call_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _python_call_name(node.value)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    return ""


def _compact_context(context: dict[str, Any], manifest: dict[str, Any]) -> dict[str, Any]:
    artifacts = context.get("artifacts", {})
    architecture = artifacts.get("architecture_revision") or artifacts.get("architecture_plan") or {}
    clarification = artifacts.get("clarification_spec") or {}
    review = artifacts.get("code_review_report") or {}
    acceptance = artifacts.get("acceptance_report") or {}
    return {
        "requirement_id": context.get("requirement_id"),
        "title": _clip_text(context.get("title"), 500),
        "description": _clip_text(context.get("description"), 12_000),
        "repositories": manifest.get("repositories", []),
        "conversation": [
            {
                "author_type": item.get("author_type"),
                "stage": item.get("stage"),
                "body": _clip_text(item.get("body"), 2_000),
            }
            for item in context.get("conversation", [])[-6:]
            if isinstance(item, dict)
        ],
        "previous_attempt_failure": {
            "error_code": _clip_text(context.get("_previous_attempt_failure", {}).get("error_code"), 80),
            "error_message": _clip_text(context.get("_previous_attempt_failure", {}).get("error_message"), 3_800),
        }
        if isinstance(context.get("_previous_attempt_failure"), dict)
        else {},
        "previous_attempt_changed_paths": [
            _clip_text(path, 1_000)
            for path in context.get("_previous_attempt_failure", {}).get("changed_paths", [])[:200]
            if isinstance(path, str)
        ]
        if isinstance(context.get("_previous_attempt_failure"), dict)
        and isinstance(context.get("_previous_attempt_failure", {}).get("changed_paths"), list)
        else [],
        "prior_commit_available": bool(artifacts.get("development_commit_manifest")),
        "artifacts": {
            "clarification_spec": _select_fields(
                clarification,
                "summary",
                "functional_requirements",
                "acceptance_criteria",
                "out_of_scope",
            ),
            "architecture": _select_fields(
                architecture,
                "target_architecture",
                "data_flow",
                "repositories",
                "test_strategy",
                "security_considerations",
            ),
            "code_review_report": _select_fields(
                review,
                "approved",
                "summary",
                "findings",
                "plan_compliance",
                "test_assessment",
            ),
            "acceptance_report": _select_fields(acceptance, "approved", "summary", "criteria"),
        },
    }


def _select_fields(value: Any, *fields: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return {field: value[field] for field in fields if field in value}


def _clip_text(value: Any, limit: int) -> str:
    return str(value or "")[:limit]


def _recoverable_missing_previous_paths(
    context: dict[str, Any],
    sandbox: WorkspaceSandbox,
) -> list[str]:
    failure = context.get("previous_attempt_failure", {})
    detail = str(failure.get("error_message") or "").casefold() if isinstance(failure, dict) else ""
    missing_markers = (
        "cannot find module",
        "no such file",
        "file is missing",
        "not found",
        "找不到模块",
        "文件缺失",
        "不存在",
    )
    if not any(marker in detail for marker in missing_markers):
        return []
    recoverable: list[str] = []
    for path in context.get("previous_attempt_changed_paths", []):
        if not isinstance(path, str):
            continue
        try:
            sandbox.read_file(path)
            continue
        except SandboxViolation:
            pass
        stem = Path(path).stem.casefold()
        name = Path(path).name.casefold()
        if name in detail or (len(stem) >= 4 and stem in detail):
            recoverable.append(path)
    return recoverable[:20]


def _mandatory_review_changes(context: dict[str, Any]) -> list[str]:
    review = context.get("artifacts", {}).get("code_review_report", {})
    findings = review.get("findings", []) if isinstance(review, dict) else []
    return [
        str(item.get("required_change"))
        for item in findings
        if isinstance(item, dict)
        and item.get("severity") in {"blocker", "high"}
        and item.get("required_change")
    ][:20]


def _required_test_review_changes(context: dict[str, Any]) -> list[str]:
    review = context.get("artifacts", {}).get("code_review_report", {})
    findings = review.get("findings", []) if isinstance(review, dict) else []
    markers = (
        "unit test",
        "unit tests",
        "test case",
        "test cases",
        "regression test",
        "单元测试",
        "测试用例",
        "回归测试",
    )
    required: list[str] = []
    for item in findings:
        if not isinstance(item, dict):
            continue
        change = str(item.get("required_change") or "").strip()
        lowered = change.casefold()
        if change and any(marker in lowered for marker in markers):
            required.append(change)
    return required[:20]


def _is_test_file_path(path: str) -> bool:
    normalized = path.replace("\\", "/").casefold()
    name = normalized.rsplit("/", 1)[-1]
    return (
        "/tests/" in f"/{normalized}/"
        or name.startswith("test_")
        or name.endswith("_test.py")
        or any(marker in name for marker in (".test.", ".spec."))
    )


def _is_test_execution_command(argv: list[str]) -> bool:
    if not argv:
        return False
    executable = Path(argv[0]).name.casefold()
    lowered = [str(item).casefold() for item in argv[1:5]]
    if executable == "pytest":
        return True
    if executable in {"npm", "pnpm", "yarn"}:
        return bool(lowered) and (
            lowered[0] == "test"
            or (lowered[0] == "run" and len(lowered) > 1 and lowered[1].startswith("test"))
        )
    if executable == "npx":
        return any(item in {"vitest", "jest"} for item in lowered)
    if executable == "node":
        return lowered[:1] == ["--test"]
    if executable == "go":
        return lowered[:1] == ["test"]
    if executable == "cargo":
        return lowered[:1] == ["test"]
    return False


def _markdown_quality_issues(sandbox: WorkspaceSandbox, paths: set[str]) -> list[str]:
    issues: list[str] = []
    for path in sorted(item for item in paths if item.lower().endswith((".md", ".markdown"))):
        try:
            lines = sandbox.read_file(path).splitlines()
        except SandboxViolation:
            continue
        headings: dict[str, list[int]] = {}
        parsed: list[tuple[int, int, str]] = []
        for index, line in enumerate(lines):
            match = re.match(r"^(#{1,6})\s+(.+?)\s*#*\s*$", line)
            if not match:
                continue
            level = len(match.group(1))
            title = re.sub(r"\s+", " ", match.group(2)).strip().casefold()
            headings.setdefault(title, []).append(index + 1)
            parsed.append((index, level, title))
        for title, line_numbers in headings.items():
            if len(line_numbers) > 1:
                issues.append(f"{path}: duplicate heading '{title}' at lines {line_numbers}")
        for position, (line_index, level, title) in enumerate(parsed[:-1]):
            next_index, next_level, _ = parsed[position + 1]
            between = [line.strip() for line in lines[line_index + 1 : next_index] if line.strip()]
            if not between and next_level <= level:
                issues.append(f"{path}: empty heading '{title}' at line {line_index + 1}")
    return issues[:20]


def _is_validation_command(argv: list[str]) -> bool:
    if not argv:
        return False
    executable = Path(argv[0]).name
    lowered = [item.lower() for item in argv[1:]]
    if executable == "pytest" or (executable in {"python", "python3"} and lowered[:2] == ["-m", "pytest"]):
        return True
    if executable in {"npm", "pnpm", "yarn"}:
        validation_prefixes = ("test", "typecheck", "lint", "check", "build", "package", "smoke")
        if not lowered or lowered[0] in {"exec", "dlx"}:
            return False
        script = lowered[1] if lowered[0] == "run" and len(lowered) > 1 else lowered[0]
        return script.startswith(validation_prefixes)
    if executable == "npx":
        local_tool = next((item for item in lowered if not item.startswith("-")), "")
        return local_tool in {"tsc", "eslint", "vitest", "jest", "prettier"}
    if executable == "node":
        return lowered[:1] == ["--test"]
    if executable == "go":
        return lowered[:1] == ["test"]
    if executable == "cargo":
        return lowered[:1] in (["test"], ["check"])
    if executable == "make":
        return any(item in {"test", "check", "lint"} for item in lowered[:2])
    if executable in {"python", "python3"} and "-c" in argv:
        script = argv[argv.index("-c") + 1] if len(argv) > argv.index("-c") + 1 else ""
        return any(marker in script for marker in ("assert ", "assert(", "sys.exit", "raise SystemExit", "raise AssertionError"))
    return False


def _changed_repository_ids(mutated_paths: set[str], repositories: list[dict[str, Any]]) -> list[str]:
    changed: list[str] = []
    for repository in repositories:
        repository_id = str(repository.get("repository_id", ""))
        relative_path = str(repository.get("relative_path") or repository_id).strip("/")
        if repository_id and any(path == relative_path or path.startswith(f"{relative_path}/") for path in mutated_paths):
            changed.append(repository_id)
    return changed
