import json
from pathlib import Path

import pytest

from app.agents.providers import (
    FakeLLMProvider,
    ModelImage,
    OpenAICompatibleProvider,
)
from app.agents.runtime import AgentRuntime
from app.services.artifacts import ArtifactStore


PNG = b"\x89PNG\r\n\x1a\n" + b"visual requirement"


class CapturingImageProvider(FakeLLMProvider):
    supports_image_input = True

    def __init__(self) -> None:
        self.user = ""
        self.images: list[ModelImage] = []

    async def complete_with_images(
        self,
        system: str,
        user: str,
        images: list[ModelImage],
    ):
        self.user = user
        self.images = images
        return await super().complete(system, user)


@pytest.mark.asyncio
async def test_agent_runtime_loads_images_without_exposing_storage_paths(tmp_path: Path) -> None:
    relative, digest, size = ArtifactStore(tmp_path).write("req-1", "input-image-1", PNG)
    provider = CapturingImageProvider()
    context = {
        "title": "Screenshot requirement",
        "description": "Use the screenshot as reference.",
        "attachments": [
            {
                "filename": "screen.png",
                "media_type": "image/png",
                "path": relative,
                "sha256": digest,
                "size_bytes": size,
            }
        ],
    }

    await AgentRuntime(provider, tmp_path).run("clarify", context)

    assert len(provider.images) == 1
    assert provider.images[0].data_url.startswith("data:image/png;base64,")
    sent_context = json.loads(provider.user)
    assert sent_context["attachments"][0]["filename"] == "screen.png"
    assert "path" not in sent_context["attachments"][0]


@pytest.mark.asyncio
async def test_openai_chat_request_uses_multimodal_user_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict = {}

    class Response:
        status_code = 200

        @staticmethod
        def json() -> dict:
            return {
                "choices": [{"message": {"content": '{"ok": true}'}}],
                "usage": {},
                "model": "gpt-5.6-sol",
            }

    class Client:
        def __init__(self, **kwargs):
            del kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            del args

        async def post(self, url: str, **kwargs):
            captured["url"] = url
            captured["body"] = kwargs["json"]
            return Response()

    monkeypatch.setattr("app.agents.providers.httpx.AsyncClient", Client)
    provider = OpenAICompatibleProvider(
        "https://api.openai.com/v1",
        "test-key",
        "gpt-5.6-sol",
        vision_enabled=True,
        thinking_enabled=None,
        max_tokens_field="max_completion_tokens",
    )

    await provider.complete_with_images(
        "Return JSON",
        '{"description":"see screenshot"}',
        [ModelImage("screen.png", "image/png", "data:image/png;base64,AAAA")],
    )

    content = captured["body"]["messages"][1]["content"]
    assert content[0] == {"type": "text", "text": '{"description":"see screenshot"}'}
    assert content[1] == {
        "type": "image_url",
        "image_url": {"url": "data:image/png;base64,AAAA", "detail": "auto"},
    }
