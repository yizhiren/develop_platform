from pathlib import Path

import pytest

from app.agents.providers import FakeLLMProvider, OpenAICompatibleProvider
from app.core.config import Settings
from app.worker import build_runtimes


def test_agent_model_profiles_inherit_shared_values_and_allow_overrides() -> None:
    settings = Settings(
        _env_file=None,
        llm_provider="deepseek",
        llm_base_url="https://shared.example/v1",
        llm_model="shared-model",
        deepseek_api_key="shared-key",
        agent2_llm_base_url="https://architect.example/v1",
        agent2_llm_model="architect-model",
        agent2_llm_api_key="architect-key",
        agent3_llm_provider="fake",
    )

    agent1 = settings.agent_model_config("agent1")
    assert (agent1.provider, agent1.base_url, agent1.model, agent1.api_key) == (
        "deepseek",
        "https://shared.example/v1",
        "shared-model",
        "shared-key",
    )
    agent2 = settings.agent_model_config("agent2")
    assert (agent2.base_url, agent2.model, agent2.api_key) == (
        "https://architect.example/v1",
        "architect-model",
        "architect-key",
    )
    assert settings.agent_model_config("agent3").provider == "fake"
    assert "shared-key" not in repr(agent1)
    assert "architect-key" not in repr(agent2)
    with pytest.raises(ValueError):
        settings.agent_model_config("agent5")


def test_worker_builds_four_runtimes_and_reads_shared_key_file_once(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    key_file = tmp_path / "shared-model-key"
    key_file.write_text("shared-secret")
    settings = Settings(
        _env_file=None,
        llm_provider="deepseek",
        llm_base_url="https://api.deepseek.example",
        llm_model="shared-model",
        deepseek_api_key_file=key_file,
        agent2_llm_model="architect-model",
        agent2_llm_api_key="architect-secret",
        agent3_llm_provider="fake",
        agent4_llm_model="acceptance-model",
    )
    monkeypatch.setattr("app.worker.get_settings", lambda: settings)

    runtimes = build_runtimes()

    assert set(runtimes) == {"agent1", "agent2", "agent3", "agent4"}
    assert isinstance(runtimes["agent1"].provider, OpenAICompatibleProvider)
    assert isinstance(runtimes["agent2"].provider, OpenAICompatibleProvider)
    assert isinstance(runtimes["agent3"].provider, FakeLLMProvider)
    assert isinstance(runtimes["agent4"].provider, OpenAICompatibleProvider)
    assert runtimes["agent1"].provider.api_key == "shared-secret"
    assert runtimes["agent2"].provider.api_key == "architect-secret"
    assert runtimes["agent2"].provider.model == "architect-model"
    assert runtimes["agent4"].provider.model == "acceptance-model"
    assert not key_file.exists()
