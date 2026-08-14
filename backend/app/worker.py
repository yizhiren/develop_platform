import asyncio
import json
import logging
import os
import socket
from pathlib import Path

from redis.asyncio import Redis

from .agents.acceptance import ACCEPTANCE_ROLES, AcceptanceToolLoop, verification_manifest
from .agents.clarification import ClarificationWorkspaceMissing, PiClarificationToolLoop
from .agents.pi_bridge import PiAgentCoreBridge
from .agents.pi_acceptance import PiAcceptanceToolLoop
from .agents.pi_developer import PiDeveloperToolLoop
from .agents.providers import FakeLLMProvider, OpenAICompatibleProvider
from .agents.coding import DeveloperToolLoop
from .agents.roles import AGENT_KEYS, agent_key_for_role
from .agents.runtime import AgentOutputError, AgentRuntime, ModelProviderError
from .agents.structured import PI_STRUCTURED_ROLES, PiStructuredRoleLoop
from .agents.verification import run_recorded_tests
from .core.config import get_settings
from .services.leases import TaskLease
from .services.diagnostics import safe_error_message
from .services.task_progress import publish_task_running


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("forgeflow.worker")


class DeveloperWorkspaceMissing(AgentOutputError):
    code = "agent.workspace_missing"
    retryable = False


class AcceptanceWorkspaceMissing(AgentOutputError):
    code = "agent.verification_workspace_missing"
    retryable = False


def _agent_failure_result(task_id: str, exc: Exception, model: str = "") -> dict:
    message = safe_error_message(exc) or "Agent failed without an error message"
    code = str(getattr(exc, "code", "agent.invalid_output"))[:80]
    logger.error("agent task failed: task_id=%s code=%s detail=%s", task_id, code, message)
    result = {
        "task_id": task_id,
        "status": "failed",
        "error_code": code,
        "error_message": message,
        "retryable": bool(getattr(exc, "retryable", False)),
        "token_usage": int(getattr(exc, "token_usage", 0)),
    }
    if model:
        result["model"] = model
    diagnostics = getattr(exc, "diagnostics", None)
    if isinstance(diagnostics, dict):
        result["diagnostics"] = diagnostics
    changed_paths = getattr(exc, "changed_paths", None)
    if isinstance(changed_paths, list):
        result["changed_paths"] = [str(path)[:1_000] for path in changed_paths[:200]]
    return result


def build_runtimes() -> dict[str, AgentRuntime]:
    settings = get_settings()
    secret_cache: dict[Path, str] = {}
    runtimes: dict[str, AgentRuntime] = {}
    for agent_key in AGENT_KEYS:
        profile = settings.agent_model_config(agent_key)
        model_api_key = _read_model_api_key(
            profile.api_key,
            profile.api_key_file,
            secret_cache,
        )
        if profile.provider == "fake":
            runtimes[agent_key] = AgentRuntime(FakeLLMProvider(), settings.artifact_root)
        else:
            is_openai = profile.provider == "openai"
            runtimes[agent_key] = AgentRuntime(
                OpenAICompatibleProvider(
                    profile.base_url,
                    model_api_key,
                    profile.model,
                    thinking_enabled=False if profile.provider == "deepseek" else None,
                    reasoning_effort="none" if is_openai else None,
                    max_tokens_field=(
                        "max_completion_tokens" if is_openai else "max_tokens"
                    ),
                    vision_enabled=is_openai,
                ),
                settings.artifact_root,
            )
    return runtimes


def build_runtime() -> AgentRuntime:
    """Backward-compatible helper for callers that only need Agent1."""
    return build_runtimes()["agent1"]


def _read_model_api_key(
    direct_value: str,
    key_file: Path | None,
    cache: dict[Path, str] | None = None,
) -> str:
    if direct_value:
        return direct_value
    if key_file is None:
        return ""
    resolved = key_file.resolve()
    if cache is not None and resolved in cache:
        return cache[resolved]
    try:
        value = key_file.read_text().strip()
    finally:
        key_file.unlink(missing_ok=True)
    if cache is not None:
        cache[resolved] = value
    return value


def _should_run_developer_tool_loop(
    role: str,
    context: dict,
    repository_automation_enabled: bool,
) -> bool:
    if role != "develop":
        return False
    has_workspace = bool(context.get("artifacts", {}).get("workspace_manifest"))
    if repository_automation_enabled and not has_workspace:
        raise DeveloperWorkspaceMissing(
            "repository automation requires a real developer workspace"
        )
    return has_workspace


def _should_run_acceptance_tool_loop(
    role: str,
    context: dict,
    repository_automation_enabled: bool,
) -> bool:
    if role not in ACCEPTANCE_ROLES:
        return False
    has_workspace = bool(verification_manifest(context, role).get("workspace_root"))
    if repository_automation_enabled and not has_workspace:
        raise AcceptanceWorkspaceMissing(
            "repository automation requires a clean verification workspace for Agent4"
        )
    return has_workspace


def _should_run_pi_clarifier(
    role: str,
    context: dict,
    provider: object,
    enabled: bool,
    repository_automation_enabled: bool,
) -> bool:
    if role != "clarify" or not enabled or not isinstance(provider, OpenAICompatibleProvider):
        return False
    repositories = context.get("repositories")
    has_repositories = isinstance(repositories, list) and bool(repositories)
    analysis = context.get("artifacts", {}).get("repository_analysis")
    has_workspace = isinstance(analysis, dict) and bool(analysis.get("workspace_root"))
    if has_repositories and not has_workspace:
        if repository_automation_enabled:
            raise ClarificationWorkspaceMissing(
                "repository automation requires a fresh read-only workspace for Agent1"
            )
        return False
    return True


def _should_run_pi_structured_role(
    role: str,
    provider: object,
    enabled: bool,
) -> bool:
    return (
        enabled
        and role in PI_STRUCTURED_ROLES
        and isinstance(provider, OpenAICompatibleProvider)
    )


async def run_worker() -> None:
    settings = get_settings()
    redis = Redis.from_url(settings.redis_url, decode_responses=True)
    worker_id = f"{socket.gethostname()}:{os.getpid()}"
    group = "forgeflow-agent-workers"
    stream_name = "forgeflow:agent-tasks"
    try:
        await redis.xgroup_create(stream_name, group, id="0", mkstream=True)
    except Exception as exc:
        if "BUSYGROUP" not in str(exc):
            raise
    runtimes = build_runtimes()
    pi_bridge = PiAgentCoreBridge(
        settings.pi_agent_core_bridge_path,
        settings.pi_agent_core_timeout_seconds,
    )
    logger.info("worker started: %s", worker_id)
    while True:
        claimed = await redis.xautoclaim(stream_name, group, worker_id, settings.task_lease_seconds * 1000, "0-0", count=1)
        messages = [(stream_name, claimed[1])] if claimed[1] else await redis.xreadgroup(group, worker_id, {stream_name: ">"}, count=1, block=5_000)
        for stream, entries in messages:
            for message_id, fields in entries:
                envelope = json.loads(fields["payload"])
                lease = TaskLease(
                    redis,
                    envelope["task_id"],
                    envelope["payload"]["requirement_id"],
                    worker_id,
                    settings.task_lease_seconds,
                    settings.max_parallel_requirements,
                    on_heartbeat=lambda task_id=envelope["task_id"]: publish_task_running(
                        redis,
                        task_id,
                        worker_id,
                        settings.task_lease_seconds,
                    ),
                )
                if not await lease.acquire():
                    await redis.xack(stream, group, message_id)
                    continue
                task_type = envelope["task_type"]
                if not task_type.startswith("agent."):
                    logger.error("unexpected task routed to agent stream: %s", task_type)
                    await redis.xack(stream, group, message_id)
                    await lease.complete()
                    continue
                role = task_type.split(".", 1)[1]
                runtime = runtimes[agent_key_for_role(role)]
                context = envelope["payload"].get("context", {})
                try:
                    if _should_run_pi_clarifier(
                        role,
                        context,
                        runtime.provider,
                        settings.pi_agent_core_enabled,
                        settings.repository_automation_enabled,
                    ):
                        output, response = await PiClarificationToolLoop(
                            runtime.provider,
                            pi_bridge,
                            settings.workspace_root,
                            settings.pi_clarifier_max_turns,
                        ).run(context)
                    elif _should_run_pi_structured_role(
                        role,
                        runtime.provider,
                        settings.pi_agent_core_enabled,
                    ):
                        output, response = await PiStructuredRoleLoop(
                            runtime.provider,
                            pi_bridge,
                            settings.workspace_root,
                            role,
                            settings.pi_structured_role_max_turns,
                        ).run(context)
                    elif _should_run_developer_tool_loop(
                        role,
                        context,
                        settings.repository_automation_enabled,
                    ):
                        if (
                            settings.pi_agent_core_enabled
                            and isinstance(runtime.provider, OpenAICompatibleProvider)
                        ):
                            output, response = await PiDeveloperToolLoop(
                                runtime.provider,
                                pi_bridge,
                                settings.pi_developer_max_turns,
                            ).run(context)
                        else:
                            output, response = await DeveloperToolLoop(runtime.provider).run(context)
                    else:
                        verification = []
                        if role in ACCEPTANCE_ROLES:
                            verification = await asyncio.to_thread(run_recorded_tests, context)
                            if verification:
                                context = {**context, "runtime_verification": verification}
                        if _should_run_acceptance_tool_loop(
                            role,
                            context,
                            settings.repository_automation_enabled,
                        ):
                            if (
                                settings.pi_agent_core_enabled
                                and isinstance(runtime.provider, OpenAICompatibleProvider)
                            ):
                                output, response = await PiAcceptanceToolLoop(
                                    runtime.provider,
                                    pi_bridge,
                                    role,
                                    settings.pi_acceptance_max_turns,
                                ).run(context)
                            else:
                                output, response = await AcceptanceToolLoop(
                                    runtime.provider,
                                    role,
                                    images=runtime.load_images(context),
                                ).run(context)
                        else:
                            output, response = await runtime.run(role, context)
                        if verification and any(item.get("status") != "passed" for item in verification):
                            output.approved = False
                            output.summary = f"平台复跑测试失败，禁止验收通过。{output.summary}"
                    result = {
                        "task_id": envelope["task_id"],
                        "status": "completed",
                        "output": output.model_dump(mode="json"),
                        "token_usage": response.prompt_tokens + response.completion_tokens,
                        "model": response.model,
                    }
                    if response.diagnostics is not None:
                        result["diagnostics"] = response.diagnostics
                except (ModelProviderError, AgentOutputError) as exc:
                    result = _agent_failure_result(
                        envelope["task_id"],
                        exc,
                        str(getattr(runtime.provider, "model", "")),
                    )
                except Exception as exc:
                    logger.exception("unhandled agent task failure")
                    result = {
                        "task_id": envelope["task_id"],
                        "status": "failed",
                        "error_code": "agent.internal",
                        "error_message": safe_error_message(exc) or "unexpected Agent worker failure",
                        "retryable": True,
                        "model": str(getattr(runtime.provider, "model", "")),
                    }
                await redis.xadd(
                    "forgeflow:results",
                    {"payload": json.dumps(result, ensure_ascii=False)},
                    maxlen=10_000,
                    approximate=True,
                )
                await redis.xack(stream, group, message_id)
                await lease.complete()


def main() -> None:
    asyncio.run(run_worker())


if __name__ == "__main__":
    main()
