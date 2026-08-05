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


class LLMProvider(ABC):
    @abstractmethod
    async def complete(self, system: str, user: str) -> ModelResponse: ...


class FakeLLMProvider(LLMProvider):
    async def complete(self, system: str, user: str) -> ModelResponse:
        del user
        if "ClarificationSpec" in system:
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
                "schema_version": "1.0", "current_state": "已分析",
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
    def __init__(self, base_url: str, api_key: str, model: str, timeout: float = 120.0, max_tokens: int = 4096, thinking_enabled: bool | None = False):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.max_tokens = max_tokens
        self.thinking_enabled = thinking_enabled

    async def complete(self, system: str, user: str) -> ModelResponse:
        if not self.api_key:
            raise ModelProviderError("model.missing_api_key", "model API key is not configured")
        body: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "response_format": {"type": "json_object"},
            "stream": False,
            "max_tokens": self.max_tokens,
        }
        if self.thinking_enabled is not None:
            body["thinking"] = {"type": "enabled" if self.thinking_enabled else "disabled"}
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
