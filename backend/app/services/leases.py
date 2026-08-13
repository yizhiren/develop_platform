from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable

from redis.asyncio import Redis


logger = logging.getLogger(__name__)


ACQUIRE_REQUIREMENT = """
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', ARGV[1])
if redis.call('ZCARD', KEYS[1]) >= tonumber(ARGV[3]) then
  return 0
end
if not redis.call('SET', KEYS[2], ARGV[4], 'NX', 'EX', ARGV[2]) then
  return 0
end
redis.call('ZADD', KEYS[1], tonumber(ARGV[1]) + tonumber(ARGV[2]), ARGV[5])
return 1
"""

REFRESH_LEASE = """
if redis.call('GET', KEYS[1]) ~= ARGV[1] or redis.call('GET', KEYS[2]) ~= ARGV[1] then
  return 0
end
redis.call('EXPIRE', KEYS[1], ARGV[2])
redis.call('EXPIRE', KEYS[2], ARGV[2])
redis.call('ZADD', KEYS[3], tonumber(ARGV[3]) + tonumber(ARGV[2]), ARGV[4])
return 1
"""

RELEASE_REQUIREMENT = """
if redis.call('GET', KEYS[1]) == ARGV[1] then redis.call('DEL', KEYS[1]) end
redis.call('ZREM', KEYS[2], ARGV[2])
return 1
"""


def requirement_cancellation_key(requirement_id: str) -> str:
    return f"forgeflow:requirement-cancelled:{requirement_id}"


class TaskLease:
    def __init__(
        self,
        redis: Redis,
        task_id: str,
        requirement_id: str,
        owner: str,
        lease_seconds: int,
        max_parallel_requirements: int,
        on_heartbeat: Callable[[], Awaitable[None]] | None = None,
    ):
        self.redis = redis
        self.task_id = task_id
        self.requirement_id = requirement_id
        self.owner = f"{owner}:{task_id}"
        self.lease_seconds = max(lease_seconds, 30)
        self.max_parallel_requirements = max(max_parallel_requirements, 1)
        self.on_heartbeat = on_heartbeat
        self.task_key = f"forgeflow:task-lock:{task_id}"
        self.requirement_key = f"forgeflow:requirement-lock:{requirement_id}"
        self.cancellation_key = requirement_cancellation_key(requirement_id)
        self.active_key = "forgeflow:active-requirements"
        self._heartbeat: asyncio.Task | None = None
        self._stop = asyncio.Event()

    async def acquire(self) -> bool:
        if await self.redis.exists(self.cancellation_key):
            return False
        if not await self.redis.set(self.task_key, self.owner, nx=True, ex=self.lease_seconds):
            return False
        while True:
            if await self.redis.exists(self.cancellation_key):
                return False
            now = int(time.time())
            acquired = await self.redis.eval(
                ACQUIRE_REQUIREMENT,
                2,
                self.active_key,
                self.requirement_key,
                now,
                self.lease_seconds,
                self.max_parallel_requirements,
                self.owner,
                self.requirement_id,
            )
            if acquired:
                await self._notify_heartbeat()
                self._heartbeat = asyncio.create_task(self._heartbeat_loop())
                return True
            if not await self.redis.expire(self.task_key, self.lease_seconds):
                return False
            await asyncio.sleep(0.25)

    async def complete(self) -> None:
        self._stop.set()
        if self._heartbeat:
            await asyncio.gather(self._heartbeat, return_exceptions=True)
        await self.redis.eval(
            RELEASE_REQUIREMENT,
            2,
            self.requirement_key,
            self.active_key,
            self.owner,
            self.requirement_id,
        )
        if await self.redis.get(self.task_key) == self.owner:
            await self.redis.expire(self.task_key, 24 * 3600)

    async def _heartbeat_loop(self) -> None:
        interval = max(self.lease_seconds // 3, 10)
        while True:
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=interval)
                return
            except TimeoutError:
                now = int(time.time())
                refreshed = await self.redis.eval(
                    REFRESH_LEASE,
                    3,
                    self.task_key,
                    self.requirement_key,
                    self.active_key,
                    self.owner,
                    self.lease_seconds,
                    now,
                    self.requirement_id,
                )
                if not refreshed:
                    return
                await self._notify_heartbeat()

    async def _notify_heartbeat(self) -> None:
        if self.on_heartbeat is None:
            return
        try:
            await self.on_heartbeat()
        except Exception:
            # The Redis lease remains authoritative. A transient progress-report
            # failure must not terminate work that still owns a valid lease.
            logger.exception("failed to publish task progress heartbeat")
