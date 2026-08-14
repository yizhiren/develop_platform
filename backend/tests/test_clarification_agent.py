from pathlib import Path

import pytest

from app.agents.clarification import ClarificationWorkspaceMissing, PiClarificationToolLoop
from app.agents.providers import ModelResponse, OpenAICompatibleProvider


def clarification_report(repository_ids: list[str]) -> dict:
    return {
        "schema_version": "1.0",
        "summary": "CI 失败需要按仓库现有脚本修复",
        "users_and_scenarios": ["维护者提交代码后由 CI 验证"],
        "functional_requirements": ["修复现有 CI 流程"],
        "non_functional_requirements": ["保留现有测试覆盖"],
        "acceptance_criteria": [
            {
                "id": "AC-1",
                "description": "仓库现有 CI 命令通过",
                "verification_method": "运行 package.json 中声明的 test 脚本",
                "priority": "must",
            }
        ],
        "edge_cases": ["依赖缓存为空"],
        "out_of_scope": [],
        "dependencies": ["Node.js"],
        "risks": ["CI 与本地版本差异"],
        "open_questions": [],
        "repository_ids": repository_ids,
    }


class BrowsingBridge:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.rejected_early_finish = False

    async def run(self, **kwargs) -> ModelResponse:
        handler = kwargs["handler"]
        self.calls.append("finish_clarification")
        with pytest.raises(ValueError, match="至少浏览每个关联仓库"):
            await handler(
                "finish_clarification",
                {"report": clarification_report(["repo-1", "repo-2"])},
            )
        self.rejected_early_finish = True
        for repository_id, filename in (("repo-1", "app.py"), ("repo-2", "package.json")):
            self.calls.append("list_files")
            await handler("list_files", {"path": repository_id})
            self.calls.append("read_file")
            await handler("read_file", {"path": f"{repository_id}/{filename}"})
        self.calls.append("finish_clarification")
        result = await handler(
            "finish_clarification",
            {"report": clarification_report(["repo-1", "repo-2"])},
        )
        assert result.terminate is True
        assert kwargs["terminal_tools"] == {"finish_clarification"}
        assert {tool.name for tool in kwargs["tools"]} == {
            "list_files",
            "search_text",
            "read_file",
            "finish_clarification",
        }
        return ModelResponse(
            content="",
            prompt_tokens=11,
            completion_tokens=7,
            model="deepseek-v4-flash",
        )


def provider() -> OpenAICompatibleProvider:
    return OpenAICompatibleProvider(
        "https://api.deepseek.com",
        "test-key-not-secret",
        "deepseek-v4-flash",
    )


@pytest.mark.asyncio
async def test_pi_clarifier_actively_browses_every_repository_before_finishing(
    tmp_path: Path,
) -> None:
    workspace_root = tmp_path / "workspaces"
    analysis_root = workspace_root / "requirement-analysis"
    (analysis_root / "repo-1").mkdir(parents=True)
    (analysis_root / "repo-2").mkdir()
    (analysis_root / "repo-1" / "app.py").write_text("def run():\n    return True\n")
    (analysis_root / "repo-2" / "package.json").write_text(
        '{"scripts":{"test":"node --test"}}'
    )
    bridge = BrowsingBridge()
    loop = PiClarificationToolLoop(provider(), bridge, workspace_root)
    context = {
        "title": "修复 CI",
        "description": "流水线失败",
        "repositories": [
            {"repository_id": "repo-1"},
            {"repository_id": "repo-2"},
        ],
        "artifacts": {
            "repository_analysis": {
                "workspace_root": str(analysis_root),
                "source": "trusted_read_only_checkout",
                "repositories": [
                    {"repository_id": "repo-1", "relative_path": "repo-1"},
                    {"repository_id": "repo-2", "relative_path": "repo-2"},
                ],
            }
        },
    }

    report, response = await loop.run(context)

    assert bridge.rejected_early_finish is True
    assert bridge.calls.count("read_file") == 2
    assert report.repository_ids == ["repo-1", "repo-2"]
    assert response.prompt_tokens + response.completion_tokens == 18
    assert "test-key-not-secret" not in response.content


@pytest.mark.asyncio
async def test_pi_clarifier_rejects_workspace_outside_configured_root(tmp_path: Path) -> None:
    workspace_root = tmp_path / "allowed"
    workspace_root.mkdir()
    outside = tmp_path / "outside"
    (outside / "repo-1").mkdir(parents=True)
    loop = PiClarificationToolLoop(provider(), BrowsingBridge(), workspace_root)

    with pytest.raises(ClarificationWorkspaceMissing, match="outside"):
        await loop.run(
            {
                "repositories": [{"repository_id": "repo-1"}],
                "artifacts": {
                    "repository_analysis": {
                        "workspace_root": str(outside),
                        "repositories": [
                            {"repository_id": "repo-1", "relative_path": "repo-1"}
                        ],
                    }
                },
            }
        )
