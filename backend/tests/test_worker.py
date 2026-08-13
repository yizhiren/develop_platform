import pytest

from app.agents.runtime import AgentOutputError
from app.worker import (
    AcceptanceWorkspaceMissing,
    DeveloperWorkspaceMissing,
    _agent_failure_result,
    _should_run_acceptance_tool_loop,
    _should_run_developer_tool_loop,
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


def test_running_result_contains_worker_lease_context() -> None:
    result = task_running_result("task-1", "worker-a:42", 300)

    assert result == {
        "task_id": "task-1",
        "status": "running",
        "worker_id": "worker-a:42",
        "lease_seconds": 300,
    }
