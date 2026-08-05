from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.database import Base
from app.models.entities import Evidence, WorkflowTask
from app.services.artifacts import ArtifactStore, ArtifactStoreError
from app.services.task_results import _externalize_large_evidence


def test_artifact_store_is_content_addressed_and_rejects_path_escape(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path)
    first = store.write("req-1", "delivery-diff", b"immutable evidence")
    second = store.write("req-1", "delivery-diff", b"immutable evidence")
    assert first == second
    relative, digest, size = first
    assert store.resolve(relative).read_bytes() == b"immutable evidence"
    assert len(digest) == 64
    assert size == len(b"immutable evidence")
    with pytest.raises(ArtifactStoreError):
        store.write("../escape", "diff", b"bad")
    with pytest.raises(ArtifactStoreError):
        store.resolve("../missing")


def test_large_delivery_diff_is_externalized_with_sha_metadata(monkeypatch, tmp_path: Path) -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    settings = Settings(
        _env_file=None,
        artifact_root=tmp_path / "artifacts",
        artifact_inline_max_bytes=16,
    )
    monkeypatch.setattr("app.services.task_results.get_settings", lambda: settings)
    with Session(engine) as session:
        task = WorkflowTask(
            requirement_id="req-1",
            task_type="git.publish_changes",
            idempotency_key="large-diff",
            payload_json="{}",
        )
        session.add(task)
        session.flush()
        output = {"combined_diff": "0123456789" * 10}
        _externalize_large_evidence(session, task, output)
        session.flush()
        evidence = session.scalar(select(Evidence))
        assert evidence is not None and evidence.size_bytes == 100
        assert output["combined_diff"].endswith("[full diff stored as immutable evidence]")
        assert ArtifactStore(settings.artifact_root).resolve(evidence.path).stat().st_size == 100
