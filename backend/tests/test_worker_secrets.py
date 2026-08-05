from pathlib import Path

from app.worker import _read_model_api_key


def test_model_key_file_is_deleted_immediately_after_worker_reads_it(tmp_path: Path) -> None:
    key_file = tmp_path / "model-key"
    key_file.write_text("test-secret-value")
    assert _read_model_api_key("", key_file) == "test-secret-value"
    assert not key_file.exists()
