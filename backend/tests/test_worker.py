import pytest

from app.agents.providers import FakeLLMProvider, OpenAICompatibleProvider
from app.agents.runtime import AgentOutputError
from app.worker import (
    AcceptanceWorkspaceMissing,
    DeveloperWorkspaceMissing,
    _agent_failure_result,
    _should_run_acceptance_tool_loop,
    _should_run_developer_tool_loop,
    _should_run_pi_clarifier,
    _should_run_pi_structured_role,
)
from app.services.task_progress import task_running_result


def test_repository_automation_rejects_developer_without_real_workspace() -> None:
    with pytest.raises(DeveloperWorkspaceMissing) as raised:
        _should_run_developer_tool_loop("develop", {"artifacts": {}}, True)

    assert raised.value.code == "agent.workspace_missing"
    assert raised.value.retryable is False


def test_developer_uses_tool_loop_when_workspace_manifest_exists() -> None:
    context = {
        "artifacts": {
            "workspace_manifest": {
                "workspace_root": "/workspaces/req-1",
                "repositories": [],
            }
        }
    }

    assert _should_run_developer_tool_loop("develop", context, True) is True
    assert _should_run_developer_tool_loop("develop", context, False) is True
    assert _should_run_developer_tool_loop("review", context, True) is False


def test_fake_legacy_mode_can_still_use_structured_developer_runtime_without_workspace() -> None:
    assert _should_run_developer_tool_loop("develop", {"artifacts": {}}, False) is False


def test_acceptance_uses_tool_loop_only_with_role_specific_clean_workspace() -> None:
    context = {
        "artifacts": {
            "verification_manifest": {"workspace_root": "/workspaces/req-verify"},
            "final_verification_manifest": {"workspace_root": "/workspaces/req-final"},
            "incremental_verification_manifest": {"workspace_root": "/workspaces/req-incremental"},
        }
    }

    assert _should_run_acceptance_tool_loop("accept", context, True) is True
    assert _should_run_acceptance_tool_loop("final_accept", context, True) is True
    assert _should_run_acceptance_tool_loop("regression", context, True) is True
    assert _should_run_acceptance_tool_loop("review", context, True) is False


def test_repository_automation_rejects_acceptance_without_clean_workspace() -> None:
    with pytest.raises(AcceptanceWorkspaceMissing) as raised:
        _should_run_acceptance_tool_loop("accept", {"artifacts": {}}, True)

    assert raised.value.code == "agent.verification_workspace_missing"
    assert raised.value.retryable is False


def test_legacy_acceptance_can_keep_protocol_only_runtime_without_workspace() -> None:
    assert _should_run_acceptance_tool_loop("accept", {"artifacts": {}}, False) is False


def test_deepseek_clarifier_uses_pi_when_analysis_workspace_exists() -> None:
    provider = OpenAICompatibleProvider("https://api.deepseek.com", "test", "deepseek")
    context = {
        "repositories": [{"repository_id": "repo-1"}],
        "artifacts": {"repository_analysis": {"workspace_root": "/workspaces/req-analysis"}},
    }

    assert _should_run_pi_clarifier("clarify", context, provider, True, True) is True
    assert _should_run_pi_clarifier("architect", context, provider, True, True) is False
    assert _should_run_pi_clarifier("clarify", context, provider, False, True) is False
    assert _should_run_pi_clarifier("clarify", context, FakeLLMProvider(), True, True) is False


def test_repository_clarifier_requires_analysis_workspace_when_automation_is_enabled() -> None:
    provider = OpenAICompatibleProvider("https://api.deepseek.com", "test", "deepseek")

    with pytest.raises(AgentOutputError) as raised:
        _should_run_pi_clarifier(
            "clarify",
            {"repositories": [{"repository_id": "repo-1"}], "artifacts": {}},
            provider,
            True,
            True,
        )

    assert raised.value.code == "agent.clarification_workspace_missing"


def test_agent2_structured_roles_use_pi_but_execution_roles_keep_their_safety_loops() -> None:
    provider = OpenAICompatibleProvider("https://api.deepseek.com", "test", "deepseek")

    assert _should_run_pi_structured_role("architect", provider, True) is True
    assert _should_run_pi_structured_role("revise", provider, True) is True
    assert _should_run_pi_structured_role("review", provider, True) is True
    assert _should_run_pi_structured_role("develop", provider, True) is False
    assert _should_run_pi_structured_role("accept", provider, True) is False
    assert _should_run_pi_structured_role("architect", provider, False) is False
    assert _should_run_pi_structured_role("architect", FakeLLMProvider(), True) is False


def test_agent_failure_result_preserves_safe_diagnostic_details() -> None:
    exc = AgentOutputError("developer tool loop exhausted its step budget\n(actions=read_file,run_command)")
    exc.code = "agent.step_budget_exhausted"
    exc.token_usage = 123
    exc.changed_paths = ["repo-1/value.py"]

    result = _agent_failure_result("task-1", exc, "deepseek-v4-flash")

    assert result["error_code"] == "agent.step_budget_exhausted"
    assert result["error_message"] == "developer tool loop exhausted its step budget (actions=read_file,run_command)"
    assert result["token_usage"] == 123
    assert result["model"] == "deepseek-v4-flash"
    assert result["changed_paths"] == ["repo-1/value.py"]


def test_generic_schema_failure_is_retryable_and_preserves_token_usage() -> None:
    exc = AgentOutputError("output schema mismatch", token_usage=456)

    result = _agent_failure_result("task-review", exc, "deepseek-v4-flash")

    assert result["error_code"] == "agent.invalid_output"
    assert result["retryable"] is True
    assert result["token_usage"] == 456


def test_agent_failure_result_preserves_structured_diagnostics() -> None:
    exc = AgentOutputError(
        "exhausted 50 turns",
        token_usage=789,
        diagnostics={"turns": 50, "max_turns": 50, "tool_calls": 31},
    )
    exc.code = "agent.pi_turn_budget_exhausted"
    exc.retryable = False

    result = _agent_failure_result("task-50", exc, "deepseek-v4-flash")

    assert result["retryable"] is False
    assert result["token_usage"] == 789
    assert result["diagnostics"] == {
        "turns": 50,
        "max_turns": 50,
        "tool_calls": 31,
    }


def test_running_result_contains_worker_lease_context() -> None:
    result = task_running_result("task-1", "worker-a:42", 300)

    assert result == {
        "task_id": "task-1",
        "status": "running",
        "worker_id": "worker-a:42",
        "lease_seconds": 300,
    }
