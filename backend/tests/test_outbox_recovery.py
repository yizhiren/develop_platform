import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.database import Base
from app.models.entities import OutboxEvent, WorkflowTask
from app.services.outbox import OutboxScheduler


class _FakeRedis:
    def __init__(self):
        self.messages = []

    async def xadd(self, stream, fields, **kwargs):
        self.messages.append((stream, fields, kwargs))
        return "1-0"


@pytest.mark.asyncio
async def test_redis_loss_republishes_queued_sqlite_task_after_backoff(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False, class_=Session)
    available_at = datetime.now(UTC) + timedelta(minutes=5)
    with sessions() as session:
        task = WorkflowTask(
            requirement_id="req-1",
            task_type="agent.clarify",
            idempotency_key="req-1:1:clarify",
            payload_json=json.dumps({"context": {}}),
            available_at=available_at,
        )
        session.add(task)
        session.flush()
        event = OutboxEvent(
            topic="forgeflow.tasks",
            aggregate_id="req-1",
            published=True,
            payload_json=json.dumps({"task_id": task.id, "task_type": task.task_type, "payload": {"context": {}}}),
        )
        session.add(event)
        session.commit()
        task_id, event_id = task.id, event.id

    monkeypatch.setattr("app.services.outbox.SessionLocal", sessions)
    scheduler = OutboxScheduler.__new__(OutboxScheduler)
    scheduler.redis = _FakeRedis()
    scheduler.reconcile_unpublished_tasks()
    with sessions() as session:
        assert session.get(OutboxEvent, event_id).published is False

    await scheduler.publish_pending()
    assert scheduler.redis.messages == []
    with sessions() as session:
        session.get(WorkflowTask, task_id).available_at = datetime.now(UTC) - timedelta(seconds=1)
        session.commit()
    await scheduler.publish_pending()
    assert scheduler.redis.messages[0][0] == "forgeflow:agent-tasks"
    with sessions() as session:
        assert session.get(OutboxEvent, event_id).published is True
