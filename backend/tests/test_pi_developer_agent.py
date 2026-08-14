from pathlib import Path

import pytest

from app.agents.pi_developer import PiDeveloperToolLoop
from app.agents.providers import ModelResponse, OpenAICompatibleProvider


class DeveloperBridge:
    async def run(self, **kwargs) -> ModelResponse:
        handler = kwargs["handler"]
        await handler("list_files", {})
        await handler("read_file", {"path": "value.py"})
        await handler(
            "replace_text",
            {
                "path": "value.py",
                "old": "return 41",
                "new": "return 42",
            },
        )
        command = await handler(
            "run_command",
            {"argv": ["pytest", "-q"], "cwd": "repo-1"},
        )
        assert command.observation["returncode"] == 0
        finish = await handler(
            "finish_development",
            {
                "report": {
                    "schema_version": "1.0",
                    "summary": "修复 answer 返回值",
                    "repositories_changed": ["repo-1"],
                    "commits": {},
                    "tests": [],
                    "unresolved_risks": [],
                    "files_changed": [],
                }
            },
        )
        assert finish.terminate is True
        return ModelResponse(content="", prompt_tokens=15, completion_tokens=9, model="deepseek")


@pytest.mark.asyncio
async def test_pi_developer_keeps_explicit_mutation_and_test_gates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SANDBOX_EXECUTOR_SOCKET", raising=False)
    repository = tmp_path / "repo-1"
    repository.mkdir()
    (repository / "value.py").write_text("def answer():\n    return 41\n")
    (repository / "test_value.py").write_text(
        "from value import answer\n\n\ndef test_answer():\n    assert answer() == 42\n"
    )
    context = {
        "requirement_id": "req-pi-dev",
        "title": "修复答案函数",
        "description": "answer 必须返回 42",
        "repositories": [{"repository_id": "repo-1"}],
        "artifacts": {
            "clarification_spec": {
                "functional_requirements": ["answer 返回 42"],
                "acceptance_criteria": [
                    {
                        "id": "AC-1",
                        "description": "answer 返回 42",
                        "verification_method": "pytest -q",
                        "priority": "must",
                    }
                ],
            },
            "architecture_plan": {
                "target_architecture": "只修复 value.answer",
                "repositories": [
                    {
                        "repository_id": "repo-1",
                        "changes": ["修改 value.py"],
                        "test_commands": ["pytest -q"],
                    }
                ],
                "test_strategy": ["pytest -q"],
            },
            "workspace_manifest": {
                "workspace_root": str(tmp_path),
                "repositories": [{"repository_id": "repo-1", "relative_path": "repo-1"}],
            },
        },
    }
    provider = OpenAICompatibleProvider("https://api.deepseek.com", "test", "deepseek")

    report, response = await PiDeveloperToolLoop(
        provider,
        DeveloperBridge(),
    ).run(context)

    assert (repository / "value.py").read_text() == "def answer():\n    return 42\n"
    assert report.files_changed == ["repo-1/value.py"]
    assert report.tests[0]["status"] == "passed"
    assert response.prompt_tokens + response.completion_tokens == 24
