from pathlib import Path

from app.agents.verification import run_recorded_tests


def test_acceptance_replays_recorded_tests_and_detects_regression(tmp_path: Path) -> None:
    repository = tmp_path / "repo-1"
    repository.mkdir()
    (repository / "test_value.py").write_text("def test_value():\n    assert 1 == 2\n")
    context = {
        "artifacts": {
            "workspace_manifest": {"workspace_root": str(tmp_path)},
            "development_report": {
                "tests": [{"command": ["pytest", "-q"], "cwd": "repo-1", "status": "passed"}]
            },
        }
    }
    results = run_recorded_tests(context)
    assert results[0]["status"] == "failed"
    assert results[0]["returncode"] == 1
