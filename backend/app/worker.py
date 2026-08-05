import asyncio
import json
import logging
import os
import socket
from pathlib import Path

from redis.asyncio import Redis

from .agents.providers import FakeLLMProvider, OpenAICompatibleProvider
from .agents.coding import DeveloperToolLoop
from .agents.runtime import AgentOutputError, AgentRuntime, ModelProviderError
from .agents.verification import run_recorded_tests
from .core.config import get_settings
from .services.leases import TaskLease


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("forgeflow.worker")


def build_runtime() -> AgentRuntime:
    settings = get_settings()
    model_api_key = _read_model_api_key(settings.deepseek_api_key, settings.deepseek_api_key_file)
    if settings.llm_provider == "fake":
        return AgentRuntime(FakeLLMProvider())
    return AgentRuntime(
        OpenAICompatibleProvider(
            settings.llm_base_url,
            model_api_key,
            settings.llm_model,
        )
    )


def _read_model_api_key(direct_value: str, key_file: Path | None) -> str:
    if direct_value:
        return direct_value
    if key_file is None:
        return ""
    try:
        value = key_file.read_text().strip()
    finally:
        key_file.unlink(missing_ok=True)
    return value


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
    runtime = build_runtime()
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
                context = envelope["payload"].get("context", {})
                try:
                    if role == "develop" and context.get("artifacts", {}).get("workspace_manifest"):
                        output, response = await DeveloperToolLoop(runtime.provider).run(context)
                    else:
                        verification = []
                        if role in {"accept", "final_accept", "regression"}:
                            verification = await asyncio.to_thread(run_recorded_tests, context)
                            if verification:
                                context = {**context, "runtime_verification": verification}
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
                except (ModelProviderError, AgentOutputError) as exc:
                    result = {
                        "task_id": envelope["task_id"],
                        "status": "failed",
                        "error_code": getattr(exc, "code", "agent.invalid_output"),
                        "retryable": getattr(exc, "retryable", False),
                    }
                except Exception:
                    logger.exception("unhandled agent task failure")
                    result = {
                        "task_id": envelope["task_id"],
                        "status": "failed",
                        "error_code": "agent.internal",
                        "retryable": True,
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
