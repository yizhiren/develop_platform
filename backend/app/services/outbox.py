import asyncio
import json
import logging
from datetime import UTC, datetime

from redis.asyncio import Redis
from sqlalchemy import select

from ..core.config import get_settings
from ..database import SessionLocal
from ..models.entities import OutboxEvent, RuntimeCursor, WorkflowTask
from .task_results import process_task_result


logger = logging.getLogger(__name__)


class OutboxScheduler:
    def __init__(self) -> None:
        self.settings = get_settings()
        self.redis = Redis.from_url(self.settings.redis_url, decode_responses=True)
        self._stop = asyncio.Event()
        self._result_cursor = self._load_result_cursor()

    async def run(self) -> None:
        self.reconcile_unpublished_tasks()
        while not self._stop.is_set():
            try:
                await self.publish_pending()
                await self.consume_results()
            except Exception:
                logger.exception("scheduler iteration failed")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=1.0)
            except TimeoutError:
                pass

    async def close(self) -> None:
        self._stop.set()
        await self.redis.aclose()

    def _load_result_cursor(self) -> str:
        with SessionLocal() as session:
            cursor = session.get(RuntimeCursor, "worker-results")
            return cursor.value if cursor else "0-0"

    def reconcile_unpublished_tasks(self) -> None:
        """Make queued SQLite tasks publishable again after Redis/data-plane loss."""
        with SessionLocal() as session:
            events = session.scalars(select(OutboxEvent).where(OutboxEvent.published.is_(True))).all()
            changed = False
            for event in events:
                try:
                    task_id = json.loads(event.payload_json).get("task_id")
                except (TypeError, json.JSONDecodeError):
                    continue
                task = session.get(WorkflowTask, task_id)
                if task is not None and task.status == "queued":
                    event.published = False
                    event.published_at = None
                    changed = True
            if changed:
                session.commit()

    async def publish_pending(self) -> None:
        with SessionLocal() as session:
            events = session.scalars(
                select(OutboxEvent).where(OutboxEvent.published.is_(False)).order_by(OutboxEvent.created_at).limit(100)
            ).all()
            for event in events:
                envelope = json.loads(event.payload_json)
                task = session.get(WorkflowTask, envelope.get("task_id"))
                if task is not None:
                    now = datetime.now(UTC)
                    if task.available_at.tzinfo is None:
                        now = now.replace(tzinfo=None)
                    if task.available_at > now:
                        continue
                task_type = str(envelope.get("task_type", ""))
                stream = "forgeflow:agent-tasks" if task_type.startswith("agent.") else "forgeflow:git-tasks"
                await self.redis.xadd(
                    stream,
                    {"event_id": event.id, "payload": event.payload_json},
                    maxlen=10_000,
                    approximate=True,
                )
                event.published = True
                event.published_at = datetime.now(UTC)
            session.commit()

    async def consume_results(self) -> None:
        items = await self.redis.xread(
            {"forgeflow:results": self._result_cursor}, count=50, block=1
        )
        for _stream, messages in items:
            for message_id, fields in messages:
                result = json.loads(fields["payload"])
                with SessionLocal() as session:
                    process_task_result(session, result)
                    cursor = session.get(RuntimeCursor, "worker-results")
                    if cursor is None:
                        cursor = RuntimeCursor(name="worker-results", value=message_id)
                        session.add(cursor)
                    else:
                        cursor.value = message_id
                    session.commit()
                self._result_cursor = message_id
