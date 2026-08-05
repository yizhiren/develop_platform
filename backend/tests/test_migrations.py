import os
from pathlib import Path

from sqlalchemy import create_engine, inspect, text

from app.core.config import Settings
from app.migrations import migrate
from app.services.git_workspace import GitWorkspaceManager


def test_migrations_are_versioned_and_idempotent(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'schema.db'}")
    assert migrate(engine) == [1, 2, 3]
    assert migrate(engine) == []
    tables = set(inspect(engine).get_table_names())
    assert {"schema_migrations", "requirements", "workflow_tasks"} <= tables
    with engine.connect() as connection:
        assert connection.execute(text("SELECT name FROM schema_migrations WHERE version = 1")).scalar_one() == "initial_schema"
        assert connection.execute(text("SELECT name FROM schema_migrations WHERE version = 2")).scalar_one() == "requirement_repository_pull_request_url"
        assert connection.execute(text("SELECT name FROM schema_migrations WHERE version = 3")).scalar_one() == "agent_run_stable_identity"


def test_workspace_cleanup_only_removes_expired_requirement_directories(tmp_path: Path) -> None:
    settings = Settings(_env_file=None, workspace_root=tmp_path, workspace_ttl_hours=1)
    manager = GitWorkspaceManager(settings)
    old = tmp_path / "old-req"
    current = tmp_path / "current-req"
    old.mkdir()
    current.mkdir()
    old_lease = manager.leases / old.name
    current_lease = manager.leases / current.name
    old_lease.touch()
    current_lease.touch()
    now = 10_000.0
    os.utime(old_lease, (now - 3_601, now - 3_601))
    os.utime(current_lease, (now, now))

    assert manager.cleanup_stale(now=now) == ["old-req"]
    assert not old.exists()
    assert current.exists()
