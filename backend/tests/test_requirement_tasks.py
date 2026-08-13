import json

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.main import list_requirement_tasks
from app.models.entities import (
    AgentRun,
    Project,
    ProjectMember,
    ProjectRole,
    Requirement,
    User,
    WorkflowTask,
)


def test_requirement_task_list_exposes_active_platform_and_agent_work() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = Session(engine)
    owner = User(email="owner@example.com", display_name="Owner", password_hash="x")
    session.add(owner)
    session.flush()
    project = Project(key="TASKS", name="Task visibility", owner_id=owner.id)
    session.add(project)
    session.flush()
    session.add(
        ProjectMember(
            project_id=project.id,
            user_id=owner.id,
            role=ProjectRole.OWNER,
        )
    )
    requirement = Requirement(
        project_id=project.id,
        number=1,
        title="Show task state",
        description="Expose queued and running work in the swimlane.",
        owner_id=owner.id,
    )
    session.add(requirement)
    session.flush()
    run = AgentRun(
        requirement_id=requirement.id,
        agent_key="agent3",
        role="develop",
        model="fake",
    )
    session.add(run)
    session.flush()
    platform_task = WorkflowTask(
        requirement_id=requirement.id,
        task_type="dependency.prepare",
        status="running",
        idempotency_key="req:1:dependencies",
        payload_json=json.dumps({"context": {}}),
    )
    agent_task = WorkflowTask(
        requirement_id=requirement.id,
        agent_run_id=run.id,
        task_type="agent.develop",
        status="queued",
        idempotency_key="req:1:develop",
        payload_json=json.dumps({"context": {}}),
    )
    session.add_all([platform_task, agent_task])
    session.commit()

    items = list_requirement_tasks(requirement.id, session, owner)
    by_id = {item["id"]: item for item in items}

    assert by_id[platform_task.id]["agent_run_id"] is None
    assert by_id[platform_task.id]["task_type"] == "dependency.prepare"
    assert by_id[platform_task.id]["status"] == "running"
    assert by_id[agent_task.id]["agent_run_id"] == run.id
    assert by_id[agent_task.id]["status"] == "queued"
