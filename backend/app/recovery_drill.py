from __future__ import annotations

import json
import os
import signal
from uuid import uuid4

from redis import Redis

from .core.config import get_settings


def main() -> None:
    settings = get_settings()
    redis = Redis.from_url(settings.redis_url, decode_responses=True)
    suffix = uuid4().hex
    stream = f"forgeflow:drill:{suffix}"
    group = f"drill-{suffix}"
    read_fd, write_fd = os.pipe()
    try:
        redis.xgroup_create(stream, group, id="0", mkstream=True)
        message_id = redis.xadd(stream, {"payload": json.dumps({"task_id": suffix})})
        child = os.fork()
        if child == 0:
            os.close(read_fd)
            child_redis = Redis.from_url(settings.redis_url, decode_responses=True)
            messages = child_redis.xreadgroup(group, "worker-that-will-crash", {stream: ">"}, count=1, block=5_000)
            if not messages:
                os._exit(2)
            os.write(write_fd, b"claimed")
            os.kill(os.getpid(), signal.SIGKILL)
            os._exit(3)
        os.close(write_fd)
        marker = os.read(read_fd, 32)
        _pid, status = os.waitpid(child, 0)
        if marker != b"claimed" or not os.WIFSIGNALED(status) or os.WTERMSIG(status) != signal.SIGKILL:
            raise RuntimeError("crash worker did not claim and terminate as expected")
        pending_before = redis.xpending(stream, group)
        _next_id, claimed, _deleted = redis.xautoclaim(
            stream,
            group,
            "replacement-worker",
            min_idle_time=0,
            start_id="0-0",
            count=1,
        )
        if not claimed or claimed[0][0] != message_id:
            raise RuntimeError("replacement worker did not reclaim the pending task")
        redis.xack(stream, group, message_id)
        pending_after = redis.xpending(stream, group)
        print(
            json.dumps(
                {
                    "worker_exit_signal": "SIGKILL",
                    "message_id": message_id,
                    "pending_before_reclaim": pending_before["pending"],
                    "replacement_claimed": True,
                    "pending_after_ack": pending_after["pending"],
                }
            )
        )
    finally:
        try:
            os.close(read_fd)
        except OSError:
            pass
        try:
            os.close(write_fd)
        except OSError:
            pass
        redis.delete(stream)
        redis.close()


if __name__ == "__main__":
    main()
