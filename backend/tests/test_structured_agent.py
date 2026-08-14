from pathlib import Path

import pytest

from app.agents.providers import ModelResponse, OpenAICompatibleProvider
from app.agents.structured import PiStructuredRoleLoop
from app.schemas.domain import ArchitecturePlan


class ArchitectBridge:
    def __init__(self) -> None:
        self.tool_names: set[str] = set()

    async def run(self, **kwargs) -> ModelResponse:
        self.tool_names = {tool.name for tool in kwargs["tools"]}
        handler = kwargs["handler"]
        await handler("list_files", {"path": "repo-1"})
        await handler("search_text", {"query": "test", "path": "repo-1"})
        await handler("read_file", {"path": "repo-1/package.json"})
        result = await handler(
            "finish_architecture",
            {
                "report": {
                    "schema_version": "1.0",
                    "confidence": 94,
                    "current_state": "CI 调用 npm test",
                    "target_architecture": "修复现有测试配置",
                    "data_flow": ["push -> CI -> npm test"],
                    "public_interface_changes": [],
                    "database_changes": [],
                    "repositories": [
                        {
                            "repository_id": "repo-1",
                            "purpose": "修复 CI",
                            "changes": ["更新测试配置"],
                            "test_commands": ["npm test"],
                            "depends_on": [],
                            "merge_order": 0,
                        }
                    ],
                    "security_considerations": ["不读取凭据"],
                    "migration_and_rollback": ["回滚配置提交"],
                    "test_strategy": ["npm test"],
                    "risks": [],
                }
            },
        )
        assert result.terminate is True
        return ModelResponse(content="", prompt_tokens=9, completion_tokens=6, model="deepseek")


@pytest.mark.asyncio
async def test_pi_architect_reads_repository_and_submits_schema(tmp_path: Path) -> None:
    workspace_root = tmp_path / "workspaces"
    analysis_root = workspace_root / "req-analysis"
    repository = analysis_root / "repo-1"
    repository.mkdir(parents=True)
    (repository / "package.json").write_text('{"scripts":{"test":"node --test"}}')
    bridge = ArchitectBridge()
    provider = OpenAICompatibleProvider("https://api.deepseek.com", "test", "deepseek")
    context = {
        "repositories": [{"repository_id": "repo-1"}],
        "artifacts": {
            "repository_analysis": {
                "workspace_root": str(analysis_root),
                "repositories": [{"repository_id": "repo-1", "relative_path": "repo-1"}],
            }
        },
    }

    report, response = await PiStructuredRoleLoop(
        provider,
        bridge,
        workspace_root,
        "architect",
    ).run(context)

    assert isinstance(report, ArchitecturePlan)
    assert report.confidence == 94
    assert response.prompt_tokens + response.completion_tokens == 15
    assert bridge.tool_names == {
        "list_files",
        "search_text",
        "read_file",
        "finish_architecture",
    }
