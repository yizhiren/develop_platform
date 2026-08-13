import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.database import Base
from app.models.entities import OutboxEvent, WorkflowTask
from app.services.outbox import OutboxScheduler, task_stream


class _FakeRedis:
    def __init__(self):
        self.messages = []

    async def xadd(self, stream, fields, **kwargs):
        self.messages.append((stream, fields, kwargs))
        return "1-0"


def test_dependency_tasks_use_the_network_enabled_worker_stream() -> None:
    assert task_stream("dependency.prepare") == "forgeflow:dependency-tasks"
    assert task_stream("agent.develop") == "forgeflow:agent-tasks"
    assert task_stream("git.prepare_workspaces") == "forgeflow:git-tasks"


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


@pytest.mark.asyncio
async def test_cancelled_task_is_consumed_without_redis_publication(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False, class_=Session)
    with sessions() as session:
        task = WorkflowTask(
            requirement_id="req-closed",
            task_type="agent.clarify",
            status="cancelled",
            idempotency_key="req-closed:1:clarify",
            payload_json=json.dumps({"context": {}}),
        )
        session.add(task)
        session.flush()
        event = OutboxEvent(
            topic="forgeflow.tasks",
            aggregate_id="req-closed",
            payload_json=json.dumps(
                {
                    "task_id": task.id,
                    "task_type": task.task_type,
                    "payload": {"context": {}},
                }
            ),
        )
        session.add(event)
        session.commit()
        event_id = event.id

    monkeypatch.setattr("app.services.outbox.SessionLocal", sessions)
    scheduler = OutboxScheduler.__new__(OutboxScheduler)
    scheduler.redis = _FakeRedis()
    await scheduler.publish_pending()

    assert scheduler.redis.messages == []
    with sessions() as session:
        assert session.get(OutboxEvent, event_id).published is True


def test_expired_running_task_is_requeued_after_progress_lease_expires(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False, class_=Session)
    with sessions() as session:
        task = WorkflowTask(
            requirement_id="req-running",
            task_type="agent.develop",
            status="running",
            idempotency_key="req-running:1:develop",
            payload_json=json.dumps({"context": {}}),
            lease_owner="worker-that-stopped",
            lease_expires_at=datetime.now(UTC) - timedelta(seconds=1),
        )
        session.add(task)
        session.flush()
        event = OutboxEvent(
            topic="forgeflow.tasks",
            aggregate_id="req-running",
            published=True,
            payload_json=json.dumps(
                {
                    "task_id": task.id,
                    "task_type": task.task_type,
                    "payload": {"context": {}},
                }
            ),
        )
        session.add(event)
        session.commit()
        task_id, event_id = task.id, event.id

    monkeypatch.setattr("app.services.outbox.SessionLocal", sessions)
    scheduler = OutboxScheduler.__new__(OutboxScheduler)
    scheduler.recover_expired_running_tasks()

    with sessions() as session:
        recovered_task = session.get(WorkflowTask, task_id)
        assert recovered_task.status == "queued"
        assert recovered_task.lease_owner is None
        assert recovered_task.lease_expires_at is None
        assert session.get(OutboxEvent, event_id).published is False
