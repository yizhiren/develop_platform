import pytest
from pydantic import ValidationError

from app.agents.providers import FakeLLMProvider, LLMProvider, ModelResponse
from app.agents.runtime import AgentRuntime
from app.schemas.domain import AcceptanceCriterion, ClarificationSpec, CodeReviewReport


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


@pytest.mark.asyncio
async def test_fake_reviewer_is_schema_valid() -> None:
    output, _ = await AgentRuntime(FakeLLMProvider()).run("review", {"diff": ""})
    assert isinstance(output, CodeReviewReport)
    assert output.approved is True


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
