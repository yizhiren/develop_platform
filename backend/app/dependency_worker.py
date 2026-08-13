from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
from typing import Any

from redis.asyncio import Redis

from .core.config import get_settings
from .services.dependencies import DependencyPreparationError, DependencyPreparer
from .services.diagnostics import safe_error_message
from .services.leases import TaskLease
from .services.task_progress import publish_task_running


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("forgeflow.dependency_worker")


async def execute_task(
    envelope: dict[str, Any],
    preparer: DependencyPreparer | None = None,
) -> dict[str, Any]:
    task_type = envelope.get("task_type")
    task_profiles = {
        "dependency.prepare": ("workspace_manifest", "development"),
        "dependency.prepare_verification": ("verification_manifest", "acceptance"),
        "dependency.prepare_incremental_verification": (
            "incremental_verification_manifest",
            "regression",
        ),
        "dependency.prepare_final_verification": (
            "final_verification_manifest",
            "final_acceptance",
        ),
    }
    if task_type not in task_profiles:
        raise DependencyPreparationError(
            "dependency.unsupported_task",
            f"unsupported task {task_type}",
        )
    manager = preparer or DependencyPreparer(get_settings())
    manifest_kind, scope = task_profiles[task_type]
    output = await asyncio.to_thread(
        manager.prepare,
        envelope["payload"]["context"],
        manifest_kind,
        scope,
    )
    return {"task_id": envelope["task_id"], "status": "completed", "output": output}


async def run_worker() -> None:
    settings = get_settings()
    redis = Redis.from_url(settings.redis_url, decode_responses=True)
    worker_id = f"{socket.gethostname()}:{os.getpid()}"
    stream_name = "forgeflow:dependency-tasks"
    group = "forgeflow-dependency-workers"
    try:
        await redis.xgroup_create(stream_name, group, id="0", mkstream=True)
    except Exception as exc:
        if "BUSYGROUP" not in str(exc):
            raise
    logger.info("dependency worker started: %s", worker_id)
    while True:
        claimed = await redis.xautoclaim(
            stream_name,
            group,
            worker_id,
            settings.task_lease_seconds * 1000,
            "0-0",
            count=1,
        )
        messages = (
            [(stream_name, claimed[1])]
            if claimed[1]
            else await redis.xreadgroup(
                group,
                worker_id,
                {stream_name: ">"},
                count=1,
                block=5_000,
            )
        )
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
                try:
                    result = await execute_task(envelope)
                except DependencyPreparationError as exc:
                    message = safe_error_message(exc)
                    logger.error(
                        "dependency task failed: task_id=%s code=%s detail=%s",
                        envelope["task_id"],
                        exc.code,
                        message,
                    )
                    result = {
                        "task_id": envelope["task_id"],
                        "status": "failed",
                        "error_code": exc.code,
                        "error_message": message,
                        "retryable": exc.retryable,
                    }
                except Exception as exc:
                    logger.exception("unhandled dependency task failure")
                    result = {
                        "task_id": envelope["task_id"],
                        "status": "failed",
                        "error_code": "dependency.internal",
                        "error_message": safe_error_message(exc)
                        or "unexpected dependency worker failure",
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
