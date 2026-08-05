from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy import Connection, Engine

from .database import Base


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    upgrade: Callable[[Connection], None]


def _initial_schema(connection: Connection) -> None:
    # Importing entities registers every table on Base.metadata.
    from .models import entities as _entities  # noqa: F401

    Base.metadata.create_all(connection)


def _add_pull_request_url(connection: Connection) -> None:
    columns = {
        str(row[1])
        for row in connection.exec_driver_sql("PRAGMA table_info(requirement_repositories)").fetchall()
    }
    if "pull_request_url" not in columns:
        connection.exec_driver_sql(
            "ALTER TABLE requirement_repositories ADD COLUMN pull_request_url TEXT"
        )


def _add_agent_identity(connection: Connection) -> None:
    columns = {
        str(row[1])
        for row in connection.exec_driver_sql("PRAGMA table_info(agent_runs)").fetchall()
    }
    if "agent_key" not in columns:
        connection.exec_driver_sql("ALTER TABLE agent_runs ADD COLUMN agent_key TEXT")
    connection.exec_driver_sql(
        """
        UPDATE agent_runs
        SET agent_key = CASE
            WHEN role = 'clarify' THEN 'agent1'
            WHEN role IN ('architect', 'review', 'revise') THEN 'agent2'
            WHEN role = 'develop' THEN 'agent3'
            WHEN role IN ('accept', 'final_accept', 'regression') THEN 'agent4'
            ELSE 'agent2'
        END
        WHERE agent_key IS NULL OR agent_key = ''
        """
    )


MIGRATIONS = (
    Migration(1, "initial_schema", _initial_schema),
    Migration(2, "requirement_repository_pull_request_url", _add_pull_request_url),
    Migration(3, "agent_run_stable_identity", _add_agent_identity),
)


def migrate(engine: Engine) -> list[int]:
    """Apply ordered, transactional SQLite schema migrations and return applied versions."""
    applied_now: list[int] = []
    with engine.begin() as connection:
        connection.exec_driver_sql(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        applied = {
            int(row[0])
            for row in connection.exec_driver_sql("SELECT version FROM schema_migrations").fetchall()
        }
        unknown = applied - {item.version for item in MIGRATIONS}
        if unknown:
            raise RuntimeError(f"database contains unknown migration versions: {sorted(unknown)}")
        for migration in MIGRATIONS:
            if migration.version in applied:
                continue
            migration.upgrade(connection)
            connection.exec_driver_sql(
                "INSERT INTO schema_migrations(version, name) VALUES (?, ?)",
                (migration.version, migration.name),
            )
            applied_now.append(migration.version)
    return applied_now
