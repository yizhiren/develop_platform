import sqlite3
from pathlib import Path

import pytest

from app.backup import create_backup, verify_restore
from app.core.config import Settings


def test_online_backup_can_be_verified_and_restored(monkeypatch, tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    with sqlite3.connect(source) as database:
        database.execute("CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, name TEXT)")
        database.execute("INSERT INTO schema_migrations VALUES (1, 'initial')")
        database.execute("CREATE TABLE sample(value TEXT)")
        database.execute("INSERT INTO sample VALUES ('preserved')")
    monkeypatch.setattr("app.backup.get_settings", lambda: Settings(_env_file=None, database_path=source))
    backup, _digest = create_backup(tmp_path / "backups")
    restored = tmp_path / "restored.db"
    result = verify_restore(backup, restored)
    assert result["migration_versions"] == [1]
    with sqlite3.connect(restored) as database:
        assert database.execute("SELECT value FROM sample").fetchone() == ("preserved",)
    with pytest.raises(RuntimeError, match="already exists"):
        verify_restore(backup, restored)
