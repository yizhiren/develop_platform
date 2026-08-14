import base64
import json
from pathlib import Path
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
from ..services.artifacts import ArtifactStore, ArtifactStoreError
from ..services.requirement_attachments import MAX_REQUIREMENT_IMAGE_BYTES
from .providers import LLMProvider, ModelImage, ModelProviderError, ModelResponse


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
    code = "agent.invalid_output"
    retryable = True

    def __init__(
        self,
        message: str,
        token_usage: int = 0,
        diagnostics: dict[str, Any] | None = None,
    ):
        super().__init__(message)
        self.token_usage = token_usage
        self.diagnostics = diagnostics


class AgentRuntime:
    def __init__(self, provider: LLMProvider, artifact_root: Path | None = None):
        self.provider = provider
        self.artifact_root = artifact_root

    async def run(self, role: str, context: dict[str, Any]) -> tuple[BaseModel, ModelResponse]:
        schema = ROLE_SCHEMAS.get(role)
        prompt = ROLE_PROMPTS.get(role)
        if schema is None or prompt is None:
            raise ValueError(f"unsupported agent role: {role}")
        schema_json = json.dumps(schema.model_json_schema(), ensure_ascii=False)
        system = f"{prompt}\nJSON Schema:\n{schema_json}"
        model_context = _context_without_attachment_paths(context)
        user = json.dumps(model_context, ensure_ascii=False)
        images = self.load_images(context)
        if images:
            response = await self.provider.complete_with_images(system, user, images)
        else:
            response = await self.provider.complete(system, user)
        try:
            parsed = schema.model_validate_json(response.content)
        except ValidationError as exc:
            validation_detail = _validation_error_summary(exc)
            repair_system = (
                "你是 JSON 修复器。把输入修复为符合下列 JSON Schema 的单个 JSON 对象，"
                "不得改变原意。\n"
                f"当前输出校验错误：\n{validation_detail}\n"
                f"JSON Schema:\n{schema_json}"
            )
            repaired = await self.provider.complete(repair_system, response.content)
            try:
                parsed = schema.model_validate_json(repaired.content)
            except ValidationError as repair_exc:
                total_tokens = (
                    response.prompt_tokens
                    + response.completion_tokens
                    + repaired.prompt_tokens
                    + repaired.completion_tokens
                )
                raise AgentOutputError(
                    f"agent output does not match {schema.__name__} after one repair; "
                    f"validation_errors={_validation_error_summary(repair_exc)}",
                    token_usage=total_tokens,
                ) from repair_exc
            response = ModelResponse(
                content=repaired.content,
                prompt_tokens=response.prompt_tokens + repaired.prompt_tokens,
                completion_tokens=response.completion_tokens + repaired.completion_tokens,
                model=repaired.model or response.model,
            )
        return parsed, response

    def load_images(self, context: dict[str, Any]) -> list[ModelImage]:
        if not self.provider.supports_image_input or self.artifact_root is None:
            return []
        attachments = context.get("attachments")
        if not isinstance(attachments, list):
            return []
        store = ArtifactStore(self.artifact_root)
        images: list[ModelImage] = []
        for attachment in attachments:
            if not isinstance(attachment, dict):
                continue
            media_type = str(attachment.get("media_type") or "")
            relative_path = str(attachment.get("path") or "")
            if media_type not in {"image/png", "image/jpeg", "image/webp"} or not relative_path:
                continue
            try:
                content = store.resolve(relative_path).read_bytes()
            except (ArtifactStoreError, OSError):
                continue
            if not content or len(content) > MAX_REQUIREMENT_IMAGE_BYTES:
                continue
            images.append(
                ModelImage(
                    filename=str(attachment.get("filename") or "screenshot"),
                    media_type=media_type,
                    data_url=f"data:{media_type};base64,{base64.b64encode(content).decode()}",
                )
            )
        return images


def _context_without_attachment_paths(context: dict[str, Any]) -> dict[str, Any]:
    result = dict(context)
    attachments = context.get("attachments")
    if isinstance(attachments, list):
        result["attachments"] = [
            {key: value for key, value in attachment.items() if key != "path"}
            if isinstance(attachment, dict)
            else attachment
            for attachment in attachments
        ]
    return result


def _validation_error_summary(exc: ValidationError) -> str:
    details: list[str] = []
    for error in exc.errors(include_url=False, include_input=False)[:8]:
        location = ".".join(str(value) for value in error.get("loc", ())) or "$"
        details.append(
            f"{location}:{error.get('type', 'invalid')}:{error.get('msg', 'invalid value')}"
        )
    return "; ".join(details)[:2_000]


__all__ = ["AgentRuntime", "AgentOutputError", "ModelProviderError"]
