import hashlib
import hmac
import shutil
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from .core.config import get_settings


def create_backup(target_dir: Path) -> tuple[Path, str]:
    settings = get_settings()
    source = settings.database_path
    target_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    destination = target_dir / f"forgeflow-{stamp}.db"
    with sqlite3.connect(source) as source_db, sqlite3.connect(destination) as target_db:
        source_db.backup(target_db)
        result = target_db.execute("PRAGMA integrity_check").fetchone()
        if result != ("ok",):
            raise RuntimeError(f"backup integrity check failed: {result}")
    digest = _sha256(destination)
    destination.with_suffix(".db.sha256").write_text(f"{digest}  {destination.name}\n")
    return destination, digest


def verify_restore(backup: Path, destination: Path) -> dict[str, object]:
    checksum_file = backup.with_suffix(".db.sha256")
    if not backup.is_file() or not checksum_file.is_file():
        raise RuntimeError("backup or checksum file is missing")
    expected = checksum_file.read_text().split(maxsplit=1)[0]
    actual = _sha256(backup)
    if not expected or not hmac.compare_digest(expected, actual):
        raise RuntimeError("backup checksum mismatch")
    if destination.exists():
        raise RuntimeError("restore destination already exists")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(backup, destination)
    with sqlite3.connect(destination) as restored:
        integrity = restored.execute("PRAGMA integrity_check").fetchone()
        if integrity != ("ok",):
            raise RuntimeError(f"restored database integrity check failed: {integrity}")
        migration_versions = [row[0] for row in restored.execute("SELECT version FROM schema_migrations ORDER BY version")]
    return {"sha256": actual, "migration_versions": migration_versions, "size_bytes": destination.stat().st_size}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


if __name__ == "__main__":
    path, checksum = create_backup(Path("/backups"))
    print(f"created {path.name} sha256={checksum}")
