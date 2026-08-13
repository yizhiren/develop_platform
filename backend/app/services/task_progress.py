from __future__ import annotations

import json
from typing import Any

from redis.asyncio import Redis


RESULT_STREAM = "forgeflow:results"


def task_running_result(
    task_id: str,
    worker_id: str,
    lease_seconds: int,
) -> dict[str, Any]:
    return {
        "task_id": task_id,
        "status": "running",
        "worker_id": worker_id,
        "lease_seconds": max(int(lease_seconds), 30),
    }


async def publish_task_running(
    redis: Redis,
    task_id: str,
    worker_id: str,
    lease_seconds: int,
) -> None:
    await redis.xadd(
        RESULT_STREAM,
        {
            "payload": json.dumps(
                task_running_result(task_id, worker_id, lease_seconds),
                ensure_ascii=False,
            )
        },
        maxlen=10_000,
        approximate=True,
    )
