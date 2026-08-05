import json
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from ..schemas.domain import (
    AcceptanceReport,
    ArchitecturePlan,
    ClarificationSpec,
    CodeReviewReport,
    DevelopmentReport,
)
from .prompts import ROLE_PROMPTS
from .providers import LLMProvider, ModelProviderError, ModelResponse


T = TypeVar("T", bound=BaseModel)

ROLE_SCHEMAS: dict[str, type[BaseModel]] = {
    "clarify": ClarificationSpec,
    "architect": ArchitecturePlan,
    "revise": ArchitecturePlan,
    "develop": DevelopmentReport,
    "review": CodeReviewReport,
    "accept": AcceptanceReport,
    "final_accept": AcceptanceReport,
    "regression": AcceptanceReport,
}


class AgentOutputError(ValueError):
    pass


class AgentRuntime:
    def __init__(self, provider: LLMProvider):
        self.provider = provider

    async def run(self, role: str, context: dict[str, Any]) -> tuple[BaseModel, ModelResponse]:
        schema = ROLE_SCHEMAS.get(role)
        prompt = ROLE_PROMPTS.get(role)
        if schema is None or prompt is None:
            raise ValueError(f"unsupported agent role: {role}")
        schema_json = json.dumps(schema.model_json_schema(), ensure_ascii=False)
        system = f"{prompt}\nJSON Schema:\n{schema_json}"
        user = json.dumps(context, ensure_ascii=False)
        response = await self.provider.complete(system, user)
        try:
            parsed = schema.model_validate_json(response.content)
        except ValidationError as exc:
            repair_system = f"你是 JSON 修复器。把输入修复为符合下列 JSON Schema 的单个 JSON 对象，不得改变原意。\n{schema_json}"
            repaired = await self.provider.complete(repair_system, response.content)
            try:
                parsed = schema.model_validate_json(repaired.content)
            except ValidationError as repair_exc:
                raise AgentOutputError(f"agent output does not match {schema.__name__} after one repair") from repair_exc
            response = ModelResponse(
                content=repaired.content,
                prompt_tokens=response.prompt_tokens + repaired.prompt_tokens,
                completion_tokens=response.completion_tokens + repaired.completion_tokens,
                model=repaired.model or response.model,
            )
        return parsed, response


__all__ = ["AgentRuntime", "AgentOutputError", "ModelProviderError"]
