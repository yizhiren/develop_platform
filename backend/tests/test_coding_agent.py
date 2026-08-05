import json
from pathlib import Path

import pytest

from app.agents.coding import DeveloperToolLoop
from app.agents.providers import LLMProvider, ModelResponse


class ScriptedProvider(LLMProvider):
    def __init__(self, actions: list[dict]):
        self.actions = iter(actions)

    async def complete(self, system: str, user: str) -> ModelResponse:
        del system, user
        return ModelResponse(content=json.dumps(next(self.actions)), model="scripted")


@pytest.mark.asyncio
async def test_developer_agent_modifies_workspace_and_runs_real_test(tmp_path: Path) -> None:
    repository = tmp_path / "repo-1"
    repository.mkdir()
    (repository / "calc.py").write_text("def add(a, b):\n    return 0\n")
    actions = [
        {"action": "read_file", "path": "repo-1/calc.py"},
        {"action": "replace_text", "path": "repo-1/calc.py", "old": "return 0", "new": "return a + b"},
        {"action": "write_file", "path": "repo-1/test_calc.py", "content": "from calc import add\n\ndef test_add():\n    assert add(1, 2) == 3\n"},
        {"action": "run_command", "argv": ["pytest", "-q"], "cwd": "repo-1"},
        {
            "action": "finish",
            "report": {
                "schema_version": "1.0",
                "summary": "Fixed addition and added a regression test.",
                "repositories_changed": ["repo-1"],
                "commits": {},
                "tests": [],
                "unresolved_risks": [],
            },
        },
    ]
    context = {
        "requirement_id": "req-1",
        "title": "Fix addition",
        "description": "Return the sum",
        "artifacts": {
            "workspace_manifest": {
                "workspace_root": str(tmp_path),
                "repositories": [{"repository_id": "repo-1", "relative_path": "repo-1"}],
            }
        },
    }
    report, response = await DeveloperToolLoop(ScriptedProvider(actions)).run(context)
    assert "a + b" in (repository / "calc.py").read_text()
    assert report.tests[0]["status"] == "passed"
    assert response.model == "scripted"
