import json

from app.models.entities import WorkflowTask
from app.services.task_results import _enforce_acceptance_evidence_gate


def acceptance_task() -> WorkflowTask:
    return WorkflowTask(
        requirement_id="req-1",
        task_type="agent.accept",
        idempotency_key="req-1:accept",
        payload_json=json.dumps(
            {
                "context": {
                    "artifacts": {
                        "verification_manifest": {
                            "workspace_root": "/workspaces/req-verify",
                            "checkout_type": "published_heads",
                        },
                        "clarification_spec": {
                            "acceptance_criteria": [
                                {
                                    "id": "AC-1",
                                    "description": "Core flow works",
                                    "verification_method": "Run an integration test",
                                    "priority": "must",
                                }
                            ]
                        },
                    }
                }
            }
        ),
    )


def test_acceptance_evidence_gate_preserves_supported_approval() -> None:
    output = {
        "approved": True,
        "summary": "Independent acceptance passed",
        "criteria": [
            {
                "criterion_id": "AC-1",
                "status": "passed",
                "summary": "Integration assertion passed",
                "evidence_paths": ["agent4-test-1"],
            }
        ],
        "regression_results": [
            {
                "evidence_id": "agent4-test-1",
                "source": "agent4_independent",
                "type": "command",
                "status": "passed",
                "criterion_ids": ["AC-1"],
                "command": ["pytest", "-q"],
            }
        ],
    }

    _enforce_acceptance_evidence_gate(acceptance_task(), output)

    assert output["approved"] is True
    assert output["regression_results"][-1]["source"] == "platform_gate"
    assert output["regression_results"][-1]["status"] == "passed"


def test_acceptance_evidence_gate_forces_unsupported_approval_to_fail() -> None:
    output = {
        "approved": True,
        "summary": "Model claimed success",
        "criteria": [
            {
                "criterion_id": "AC-1",
                "status": "passed",
                "summary": "No real evidence",
                "evidence_paths": ["invented-evidence"],
            }
        ],
        "regression_results": [],
    }

    _enforce_acceptance_evidence_gate(acceptance_task(), output)

    assert output["approved"] is False
    assert output["regression_results"][-1]["status"] == "failed"
    assert "without directly linked passing evidence" in output["summary"]
    assert "at least one successful independent Agent4 command" in output["summary"]


def test_acceptance_evidence_gate_rejects_omitted_confirmed_criterion() -> None:
    output = {
        "approved": True,
        "summary": "Empty approval",
        "criteria": [],
        "regression_results": [
            {
                "evidence_id": "agent4-test-1",
                "source": "agent4_independent",
                "type": "command",
                "status": "passed",
                "criterion_ids": [],
            }
        ],
    }

    _enforce_acceptance_evidence_gate(acceptance_task(), output)

    assert output["approved"] is False
    assert "must exactly cover confirmed criteria" in output["summary"]


def test_acceptance_evidence_gate_keeps_legacy_protocol_flow_compatible() -> None:
    task = acceptance_task()
    task.payload_json = json.dumps({"context": {"artifacts": {}}})
    output = {"approved": True, "summary": "Legacy fake acceptance", "criteria": []}

    _enforce_acceptance_evidence_gate(task, output)

    assert output["approved"] is True
    assert "regression_results" not in output
