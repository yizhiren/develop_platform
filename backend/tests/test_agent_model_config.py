from hmac import compare_digest
from pathlib import Path

import pytest

from app.agents.providers import FakeLLMProvider, OpenAICompatibleProvider
from app.core.config import Settings
from app.worker import build_runtimes


@pytest.fixture(autouse=True)
def isolate_model_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Model profile tests must not consume credentials from the invoking shell."""
    shared = {
        "LLM_PROVIDER",
        "LLM_BASE_URL",
        "LLM_MODEL",
        "DEEPSEEK_API_KEY",
        "DEEPSEEK_API_KEY_FILE",
    }
    per_role = {
        f"AGENT{index}_LLM_{suffix}"
        for index in range(1, 5)
        for suffix in ("PROVIDER", "BASE_URL", "MODEL", "API_KEY", "API_KEY_FILE")
    }
    for name in shared | per_role:
        monkeypatch.delenv(name, raising=False)


def test_agent_model_profiles_inherit_shared_values_and_allow_overrides() -> None:
    settings = Settings(
        _env_file=None,
        llm_provider="deepseek",
        llm_base_url="https://shared.example/v1",
        llm_model="shared-model",
        deepseek_api_key="shared-key",
        agent1_llm_provider="",
        agent1_llm_base_url="",
        agent1_llm_model="",
        agent1_llm_api_key="",
        agent2_llm_provider="openai",
        agent2_llm_base_url="https://architect.example/v1",
        agent2_llm_model="architect-model",
        agent2_llm_api_key="architect-key",
        agent3_llm_provider="fake",
        agent3_llm_base_url="",
        agent3_llm_model="",
        agent3_llm_api_key="",
        agent4_llm_provider="",
        agent4_llm_base_url="",
        agent4_llm_model="",
        agent4_llm_api_key="",
    )

    agent1 = settings.agent_model_config("agent1")
    assert (agent1.provider, agent1.base_url, agent1.model, agent1.api_key) == (
        "deepseek",
        "https://shared.example/v1",
        "shared-model",
        "shared-key",
    )
    agent2 = settings.agent_model_config("agent2")
    assert (agent2.provider, agent2.base_url, agent2.model, agent2.api_key) == (
        "openai",
        "https://architect.example/v1",
        "architect-model",
        "architect-key",
    )
    settings.agent2_llm_api_key = ""
    assert settings.agent_model_config("agent2").api_key == ""
    assert settings.agent_model_config("agent3").provider == "fake"
    assert "shared-key" not in repr(agent1)
    assert "architect-key" not in repr(agent2)
    with pytest.raises(ValueError):
        settings.agent_model_config("agent5")


def test_all_pi_roles_default_to_thirty_two_turns() -> None:
    settings = Settings(_env_file=None)

    assert settings.pi_clarifier_max_turns == 32
    assert settings.pi_structured_role_max_turns == 32
    assert settings.pi_acceptance_max_turns == 32
    assert settings.pi_developer_max_turns == 32


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
        agent1_llm_provider="",
        agent1_llm_base_url="",
        agent1_llm_model="",
        agent1_llm_api_key="",
        agent2_llm_provider="openai",
        agent2_llm_base_url="",
        agent2_llm_model="architect-model",
        agent2_llm_api_key="architect-secret",
        agent3_llm_provider="fake",
        agent3_llm_base_url="",
        agent3_llm_model="",
        agent3_llm_api_key="",
        agent4_llm_provider="",
        agent4_llm_base_url="",
        agent4_llm_model="acceptance-model",
        agent4_llm_api_key="",
    )
    monkeypatch.setattr("app.worker.get_settings", lambda: settings)

    runtimes = build_runtimes()

    assert set(runtimes) == {"agent1", "agent2", "agent3", "agent4"}
    assert isinstance(runtimes["agent1"].provider, OpenAICompatibleProvider)
    assert isinstance(runtimes["agent2"].provider, OpenAICompatibleProvider)
    assert isinstance(runtimes["agent3"].provider, FakeLLMProvider)
    assert isinstance(runtimes["agent4"].provider, OpenAICompatibleProvider)
    # compare_digest prevents pytest assertion rewriting from echoing a real
    # environment credential if this isolation ever regresses.
    assert compare_digest(runtimes["agent1"].provider.api_key, "shared-secret")
    assert compare_digest(runtimes["agent2"].provider.api_key, "architect-secret")
    assert runtimes["agent2"].provider.model == "architect-model"
    assert runtimes["agent2"].provider.thinking_enabled is None
    assert runtimes["agent2"].provider.reasoning_effort == "none"
    assert runtimes["agent2"].provider.max_tokens_field == "max_completion_tokens"
    assert runtimes["agent2"].provider.supports_image_input is True
    assert runtimes["agent1"].provider.thinking_enabled is False
    assert runtimes["agent1"].provider.reasoning_effort is None
    assert runtimes["agent1"].provider.max_tokens_field == "max_tokens"
    assert runtimes["agent1"].provider.supports_image_input is False
    assert runtimes["agent4"].provider.model == "acceptance-model"
    assert not key_file.exists()
