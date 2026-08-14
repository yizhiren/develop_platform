from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

import httpx


class ModelProviderError(RuntimeError):
    def __init__(self, code: str, message: str, retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.retryable = retryable


@dataclass
class ModelResponse:
    content: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    model: str = ""
    diagnostics: dict[str, Any] | None = None


@dataclass(frozen=True)
class ModelImage:
    filename: str
    media_type: str
    data_url: str


class LLMProvider(ABC):
    supports_image_input = False

    @abstractmethod
    async def complete(self, system: str, user: str) -> ModelResponse: ...

    async def complete_with_images(
        self,
        system: str,
        user: str,
        images: list[ModelImage],
    ) -> ModelResponse:
        del images
        return await self.complete(system, user)


class FakeLLMProvider(LLMProvider):
    async def complete(self, system: str, user: str) -> ModelResponse:
        if "AcceptanceAction" in system:
            request = json.loads(user)
            criteria = (
                request.get("context", {})
                .get("artifacts", {})
                .get("clarification_spec", {})
                .get("acceptance_criteria", [])
            )
            criterion_ids = [str(item.get("id")) for item in criteria if item.get("id")]
            progress = request.get("progress", {})
            if not progress.get("independent_successful_test_count"):
                payload = {
                    "action": "run_command",
                    "argv": ["python", "-c", "assert True"],
                    "cwd": ".",
                    "criterion_ids": criterion_ids,
                }
            else:
                evidence_ids = [
                    item
                    for item in progress.get("available_evidence_ids", [])
                    if str(item).startswith("agent4-test-")
                ]
                evidence_id = evidence_ids[-1] if evidence_ids else ""
                failed = bool(progress.get("failed_test_count"))
                payload = {
                    "action": "finish",
                    "report": {
                        "schema_version": "1.0",
                        "approved": not failed,
                        "summary": "验收失败" if failed else "独立验收通过",
                        "criteria": [
                            {
                                "criterion_id": item,
                                "status": "failed" if failed else "passed",
                                "summary": "平台 Fake Provider 独立断言",
                                "evidence_paths": [evidence_id] if evidence_id else [],
                            }
                            for item in criterion_ids
                        ],
                        "regression_results": [],
                        "environment": {"provider": "fake"},
                    },
                }
        elif "ClarificationSpec" in system:
            payload = {
                "schema_version": "1.0",
                "summary": "已澄清的示例需求",
                "users_and_scenarios": ["项目成员使用该功能"],
                "functional_requirements": ["实现需求描述中的功能"],
                "non_functional_requirements": ["提供自动化测试"],
                "acceptance_criteria": [{
                    "id": "AC-1", "description": "核心流程可用",
                    "verification_method": "自动化端到端测试", "priority": "must"
                }],
                "edge_cases": ["外部服务暂时不可用"],
                "out_of_scope": [], "dependencies": [], "risks": [],
                "open_questions": [], "repository_ids": [],
            }
        elif "ArchitecturePlan" in system:
            payload = {
                "schema_version": "1.0", "confidence": 90, "current_state": "已分析",
                "target_architecture": "按现有模块边界实现", "data_flow": [],
                "public_interface_changes": [], "database_changes": [], "repositories": [],
                "security_considerations": ["保持最小权限"],
                "migration_and_rollback": ["保留向后兼容"],
                "test_strategy": ["运行单元与端到端测试"], "risks": [],
            }
        elif "DevelopmentReport" in system:
            payload = {"schema_version": "1.0", "summary": "实现完成", "repositories_changed": [], "commits": {}, "tests": [], "unresolved_risks": []}
        elif "CodeReviewReport" in system:
            payload = {"schema_version": "1.0", "approved": True, "summary": "评审通过", "findings": [], "plan_compliance": ["符合方案"], "test_assessment": ["测试充分"]}
        else:
            payload = {"schema_version": "1.0", "approved": True, "summary": "验收通过", "criteria": [], "regression_results": [], "environment": {"provider": "fake"}}
        return ModelResponse(content=json.dumps(payload, ensure_ascii=False), model="fake")


class OpenAICompatibleProvider(LLMProvider):
    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        timeout: float = 120.0,
        max_tokens: int = 4096,
        thinking_enabled: bool | None = False,
        reasoning_effort: str | None = None,
        max_tokens_field: str = "max_tokens",
        vision_enabled: bool = False,
    ):
        if max_tokens_field not in {"max_tokens", "max_completion_tokens"}:
            raise ValueError("unsupported max tokens field")
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.max_tokens = max_tokens
        self.thinking_enabled = thinking_enabled
        self.reasoning_effort = reasoning_effort
        self.max_tokens_field = max_tokens_field
        self.supports_image_input = vision_enabled

    async def complete(self, system: str, user: str) -> ModelResponse:
        return await self._complete(system, user, [])

    async def complete_with_images(
        self,
        system: str,
        user: str,
        images: list[ModelImage],
    ) -> ModelResponse:
        return await self._complete(system, user, images if self.supports_image_input else [])

    async def _complete(
        self,
        system: str,
        user: str,
        images: list[ModelImage],
    ) -> ModelResponse:
        if not self.api_key:
            raise ModelProviderError("model.missing_api_key", "model API key is not configured")
        user_content: str | list[dict[str, Any]] = user
        if images:
            user_content = [{"type": "text", "text": user}]
            user_content.extend(
                {
                    "type": "image_url",
                    "image_url": {"url": image.data_url, "detail": "auto"},
                }
                for image in images
            )
        body: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user_content},
            ],
            "response_format": {"type": "json_object"},
            "stream": False,
        }
        body[self.max_tokens_field] = self.max_tokens
        if self.thinking_enabled is not None:
            body["thinking"] = {"type": "enabled" if self.thinking_enabled else "disabled"}
        if self.reasoning_effort is not None:
            body["reasoning_effort"] = self.reasoning_effort
        for attempt in range(2):
            try:
                async with httpx.AsyncClient(timeout=self.timeout) as client:
                    response = await client.post(
                        f"{self.base_url}/chat/completions",
                        headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
                        json=body,
                    )
            except httpx.TimeoutException as exc:
                raise ModelProviderError("model.timeout", "model request timed out", True) from exc
            except httpx.HTTPError as exc:
                raise ModelProviderError("model.network", "model network request failed", True) from exc

            if response.status_code >= 400:
                retryable = response.status_code in {429, 500, 502, 503, 504}
                code = f"model.http_{response.status_code}"
                raise ModelProviderError(code, f"model returned HTTP {response.status_code}", retryable)
            data = response.json()
            try:
                content = data["choices"][0]["message"]["content"]
            except (KeyError, IndexError, TypeError) as exc:
                raise ModelProviderError("model.invalid_response", "missing model content", True) from exc
            if content:
                usage = data.get("usage") or {}
                return ModelResponse(
                    content=content,
                    prompt_tokens=int(usage.get("prompt_tokens", 0)),
                    completion_tokens=int(usage.get("completion_tokens", 0)),
                    model=str(data.get("model", self.model)),
                )
            if attempt == 0:
                body["messages"][0]["content"] += "\n立即输出一个完整 JSON 对象；不要输出思考过程、Markdown 或空白内容。"
        raise ModelProviderError("model.empty_response", "model returned empty content twice", True)

    async def list_models(self) -> list[str]:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.get(
                f"{self.base_url}/models", headers={"Authorization": f"Bearer {self.api_key}"}
            )
        response.raise_for_status()
        return [str(item["id"]) for item in response.json().get("data", [])]
