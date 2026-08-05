from __future__ import annotations

import asyncio
import json
from uuid import uuid4

from redis.asyncio import Redis

from .core.config import get_settings
from .services.leases import TaskLease


async def run() -> None:
    settings = get_settings()
    redis = Redis.from_url(settings.redis_url, decode_responses=True)
    suffix = uuid4().hex
    first = TaskLease(redis, f"task-a-{suffix}", f"req-a-{suffix}", "drill-a", 30, 1)
    second = TaskLease(redis, f"task-b-{suffix}", f"req-b-{suffix}", "drill-b", 30, 1)
    try:
        if not await first.acquire():
            raise RuntimeError("first lease was not acquired")
        waiting = asyncio.create_task(second.acquire())
        await asyncio.sleep(0.6)
        blocked_while_full = not waiting.done()
        await first.complete()
        acquired_after_release = await asyncio.wait_for(waiting, timeout=3)
        if not blocked_while_full or not acquired_after_release:
            raise RuntimeError("global requirement concurrency limit was not enforced")
        await second.complete()
        print(
            json.dumps(
                {
                    "max_parallel_requirements": 1,
                    "second_blocked_while_full": blocked_while_full,
                    "second_acquired_after_release": acquired_after_release,
                }
            )
        )
    finally:
        await redis.delete(first.task_key, second.task_key, first.requirement_key, second.requirement_key)
        await redis.zrem(first.active_key, first.requirement_id, second.requirement_id)
        await redis.aclose()


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
