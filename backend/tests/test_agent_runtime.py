import pytest
from pydantic import ValidationError

from app.agents.providers import FakeLLMProvider, LLMProvider, ModelResponse
from app.agents.runtime import AgentOutputError, AgentRuntime
from app.schemas.domain import AcceptanceCriterion, ArchitecturePlan, ClarificationSpec, CodeReviewReport


@pytest.mark.asyncio
async def test_fake_clarifier_is_schema_valid() -> None:
    output, response = await AgentRuntime(FakeLLMProvider()).run(
        "clarify", {"title": "test", "description": "a useful requirement"}
    )
    assert isinstance(output, ClarificationSpec)
    assert output.acceptance_criteria
    assert all(item.verification_method.strip() for item in output.acceptance_criteria)
    assert response.model == "fake"

    with pytest.raises(ValidationError):
        AcceptanceCriterion(
            id="AC-empty-method",
            description="This criterion has no executable guidance",
            verification_method="",
        )


class CapturingClarifierProvider(FakeLLMProvider):
    def __init__(self) -> None:
        self.system = ""
        self.user = ""

    async def complete(self, system: str, user: str) -> ModelResponse:
        self.system = system
        self.user = user
        return await super().complete(system, user)


@pytest.mark.asyncio
async def test_clarifier_is_told_to_use_repository_evidence_before_asking() -> None:
    provider = CapturingClarifierProvider()
    repository_analysis = {
        "repositories": [
            {
                "repository_id": "repo-1",
                "head_sha": "abc123",
                "file_tree": ["package.json", ".github/workflows/ci.yml"],
                "selected_files": [
                    {"path": "package.json", "content": '{"scripts":{"test":"npm test"}}'}
                ],
            }
        ]
    }

    await AgentRuntime(provider).run(
        "clarify",
        {
            "title": "Fix CI",
            "description": "The build fails",
            "repositories": [{"repository_id": "repo-1"}],
            "artifacts": {"repository_analysis": repository_analysis},
        },
    )

    assert "必须先检查 context.artifacts.repository_analysis" in provider.system
    assert "不得再询问用户" in provider.system
    assert '"repository_analysis"' in provider.user
    assert "npm test" in provider.user


@pytest.mark.asyncio
async def test_fake_reviewer_is_schema_valid() -> None:
    output, _ = await AgentRuntime(FakeLLMProvider()).run("review", {"diff": ""})
    assert isinstance(output, CodeReviewReport)
    assert output.approved is True


@pytest.mark.asyncio
async def test_fake_architect_reports_bounded_confidence() -> None:
    output, _ = await AgentRuntime(FakeLLMProvider()).run(
        "architect", {"title": "test", "description": "a useful requirement"}
    )
    assert isinstance(output, ArchitecturePlan)
    assert output.confidence == 90

    with pytest.raises(ValidationError):
        ArchitecturePlan.model_validate(
            {
                **output.model_dump(),
                "confidence": 101,
            }
        )


class InvalidThenValidProvider(LLMProvider):
    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, system: str, user: str) -> ModelResponse:
        self.calls += 1
        if self.calls == 1:
            return ModelResponse(content='{"approved": "not-valid"}', prompt_tokens=3, completion_tokens=2, model="stub")
        valid = await FakeLLMProvider().complete("CodeReviewReport", user)
        valid.prompt_tokens = 4
        valid.completion_tokens = 5
        return valid


@pytest.mark.asyncio
async def test_invalid_model_json_is_repaired_once() -> None:
    provider = InvalidThenValidProvider()
    output, response = await AgentRuntime(provider).run("review", {"diff": ""})
    assert output.approved is True
    assert provider.calls == 2
    assert response.prompt_tokens == 7
    assert response.completion_tokens == 7


class AlwaysInvalidProvider(LLMProvider):
    def __init__(self) -> None:
        self.system_prompts: list[str] = []

    async def complete(self, system: str, user: str) -> ModelResponse:
        self.system_prompts.append(system)
        call = len(self.system_prompts)
        return ModelResponse(
            content='{"approved": "not-valid"}',
            prompt_tokens=call + 2,
            completion_tokens=call + 1,
            model="stub",
        )


@pytest.mark.asyncio
async def test_unrepairable_output_is_retryable_and_preserves_usage_and_validation_detail() -> None:
    provider = AlwaysInvalidProvider()

    with pytest.raises(AgentOutputError) as raised:
        await AgentRuntime(provider).run("review", {"diff": ""})

    assert raised.value.retryable is True
    assert raised.value.token_usage == 12
    assert "validation_errors=" in str(raised.value)
    assert "approved" in str(raised.value)
    assert "当前输出校验错误" in provider.system_prompts[1]
    assert "approved" in provider.system_prompts[1]
