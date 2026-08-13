import pytest

from app.services.leases import TaskLease, requirement_cancellation_key


class _CancelledRedis:
    def __init__(self, cancelled_key: str):
        self.cancelled_key = cancelled_key
        self.set_called = False

    async def exists(self, key: str) -> int:
        return int(key == self.cancelled_key)

    async def set(self, *args, **kwargs):
        self.set_called = True
        return True


class _LeaseRedis:
    def __init__(self):
        self.values: dict[str, str] = {}

    async def exists(self, _key: str) -> int:
        return 0

    async def set(self, key: str, value: str, **_kwargs):
        self.values[key] = value
        return True

    async def eval(self, *_args):
        return 1

    async def get(self, key: str):
        return self.values.get(key)

    async def expire(self, _key: str, _seconds: int):
        return True


@pytest.mark.asyncio
async def test_cancelled_requirement_never_acquires_worker_lease() -> None:
    requirement_id = "req-closed"
    redis = _CancelledRedis(requirement_cancellation_key(requirement_id))
    lease = TaskLease(
        redis,  # type: ignore[arg-type]
        "task-1",
        requirement_id,
        "worker-1",
        300,
        2,
    )

    assert await lease.acquire() is False
    assert redis.set_called is False


@pytest.mark.asyncio
async def test_acquired_lease_reports_running_before_work_starts() -> None:
    redis = _LeaseRedis()
    heartbeats: list[str] = []

    async def report_running() -> None:
        heartbeats.append("running")

    lease = TaskLease(
        redis,  # type: ignore[arg-type]
        "task-1",
        "req-1",
        "worker-1",
        300,
        2,
        on_heartbeat=report_running,
    )

    assert await lease.acquire() is True
    assert heartbeats == ["running"]
    await lease.complete()
