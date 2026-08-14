from pathlib import Path

import pytest

from app.agents.pi_acceptance import PiAcceptanceToolLoop
from app.agents.providers import ModelResponse, OpenAICompatibleProvider


class AcceptanceBridge:
    async def run(self, **kwargs) -> ModelResponse:
        handler = kwargs["handler"]
        await handler("list_files", {"path": "repo-1"})
        read = await handler(
            "read_file",
            {"path": "test_value.py", "criterion_ids": ["AC-1"]},
        )
        await handler(
            "run_command",
            {
                "argv": ["rg", "answer", "value.py"],
                "cwd": "repo-1",
                "criterion_ids": ["AC-1"],
            },
        )
        command = await handler(
            "run_command",
            {
                "argv": ["python", "-c", "from value import answer; assert answer() == 42"],
                "cwd": "repo-1",
                "criterion_ids": ["AC-1"],
            },
        )
        finish = await handler(
            "finish_acceptance",
            {
                "report": {
                    "schema_version": "1.0",
                    "approved": True,
                    "summary": "独立验收通过",
                    "criteria": [
                        {
                            "criterion_id": "AC-1",
                            "status": "passed",
                            "summary": "实现与独立断言均通过",
                            "evidence_paths": [
                                read.observation["evidence_id"],
                                command.observation["evidence_id"],
                            ],
                        }
                    ],
                }
            },
        )
        assert finish.terminate is True
        return ModelResponse(content="", prompt_tokens=12, completion_tokens=8, model="deepseek")


@pytest.mark.asyncio
async def test_pi_acceptance_preserves_the_agents_conclusion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SANDBOX_EXECUTOR_SOCKET", raising=False)
    repository = tmp_path / "repo-1"
    repository.mkdir()
    (repository / "value.py").write_text("def answer():\n    return 42\n")
    (repository / "test_value.py").write_text(
        "from value import answer\n\n\ndef test_answer():\n    assert answer() == 42\n"
    )
    context = {
        "title": "验收答案函数",
        "repositories": [{"repository_id": "repo-1"}],
        "artifacts": {
            "clarification_spec": {
                "acceptance_criteria": [
                    {
                        "id": "AC-1",
                        "description": "answer 返回 42",
                        "verification_method": "运行独立 Python 断言",
                        "priority": "must",
                    }
                ]
            },
            "verification_manifest": {
                "workspace_root": str(tmp_path),
                "checkout_type": "published_heads",
                "repositories": [{"repository_id": "repo-1", "relative_path": "repo-1"}],
            },
        },
    }
    provider = OpenAICompatibleProvider("https://api.deepseek.com", "test", "deepseek")

    report, response = await PiAcceptanceToolLoop(
        provider,
        AcceptanceBridge(),
        "accept",
    ).run(context)

    assert report.approved is True
    assert report.environment["agent_core"] == "pi"
    assert any(item["type"] == "command" for item in report.regression_results)
    assert response.prompt_tokens + response.completion_tokens == 20
