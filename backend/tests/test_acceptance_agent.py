import json
from pathlib import Path

import pytest

from app.agents.acceptance import AcceptanceToolLoop, _command_completed_successfully
from app.agents.providers import FakeLLMProvider, LLMProvider, ModelImage, ModelResponse


class ScriptedProvider(LLMProvider):
    def __init__(self, actions: list[dict]):
        self.actions = iter(actions)
        self.users: list[dict] = []
        self.systems: list[str] = []

    async def complete(self, system: str, user: str) -> ModelResponse:
        self.systems.append(system)
        self.users.append(json.loads(user))
        return ModelResponse(
            content=json.dumps(next(self.actions)),
            prompt_tokens=10,
            completion_tokens=5,
            model="scripted-acceptance",
        )


def test_search_no_match_is_a_successful_acceptance_observation() -> None:
    assert _command_completed_successfully(["rg", "obsolete-symbol"], 1) is True
    assert _command_completed_successfully(["grep", "-R", "obsolete-symbol", "."], 1) is True
    assert _command_completed_successfully(["rg", "obsolete-symbol"], 2) is False


class ImageScriptedProvider(ScriptedProvider):
    supports_image_input = True

    def __init__(self, actions: list[dict]):
        super().__init__(actions)
        self.image_calls: list[list[ModelImage]] = []

    async def complete_with_images(
        self,
        system: str,
        user: str,
        images: list[ModelImage],
    ) -> ModelResponse:
        self.image_calls.append(images)
        return await self.complete(system, user)


def acceptance_context(root: Path, criteria: list[dict] | None = None) -> dict:
    return {
        "requirement_id": "req-1",
        "title": "Verify calculator",
        "description": "The calculator must add values and keep its documentation.",
        "artifacts": {
            "clarification_spec": {
                "summary": "Calculator acceptance",
                "functional_requirements": ["Add two values"],
                "acceptance_criteria": criteria
                or [
                    {
                        "id": "AC-1",
                        "description": "Addition returns the sum",
                        "verification_method": "Run the calculator unit test",
                        "priority": "must",
                    },
                    {
                        "id": "AC-2",
                        "description": "Usage is documented",
                        "verification_method": "Inspect README and assert the example exists",
                        "priority": "must",
                    },
                ],
            },
            "architecture_plan": {
                "test_strategy": ["Run pytest and inspect the documented example"],
                "repositories": [],
            },
            "development_report": {
                "tests": [{"command": ["pytest", "-q"], "cwd": "repo-1", "status": "passed"}],
            },
            "verification_manifest": {
                "workspace_root": str(root),
                "checkout_type": "published_heads",
                "repositories": [{"repository_id": "repo-1", "relative_path": "repo-1"}],
            },
        },
    }


@pytest.mark.asyncio
async def test_acceptance_agent_independently_inspects_tests_and_covers_every_criterion(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repo-1"
    repository.mkdir()
    (repository / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    (repository / "test_calc.py").write_text(
        "from calc import add\n\ndef test_add():\n    assert add(1, 2) == 3\n"
    )
    (repository / "README.md").write_text("# Calculator\n\nExample: add(1, 2) == 3\n")
    pytest_cache = repository / ".pytest_cache" / "v" / "cache"
    pytest_cache.mkdir(parents=True)
    (pytest_cache / "nodeids").write_text("[]")
    provider = ImageScriptedProvider(
        [
            {"action": "read_file", "path": "repo-1/README.md", "criterion_ids": ["AC-2"]},
            {
                "action": "run_command",
                "argv": ["pytest", "-q"],
                "cwd": "repo-1",
                "criterion_ids": ["AC-1", "AC-2"],
            },
            {
                "action": "finish",
                "report": {
                    "approved": True,
                    "summary": "All acceptance criteria passed independently.",
                    "criteria": [
                        {
                            "criterion_id": "AC-1",
                            "status": "passed",
                            "summary": "Calculator test passed.",
                            "evidence_paths": ["agent4-test-2"],
                        },
                        {
                            "criterion_id": "AC-2",
                            "status": "passed",
                            "summary": "README inspection and test assertion passed.",
                            "evidence_paths": ["agent4-read-1", "agent4-test-2"],
                        },
                    ],
                    "regression_results": [],
                    "environment": {},
                },
            },
        ]
    )

    reference = ModelImage("reference.png", "image/png", "data:image/png;base64,AAAA")
    report, response = await AcceptanceToolLoop(provider, images=[reference]).run(
        acceptance_context(tmp_path)
    )

    assert report.approved is True
    assert {item.criterion_id for item in report.criteria} == {"AC-1", "AC-2"}
    assert {item["evidence_id"] for item in report.regression_results} == {
        "agent4-read-1",
        "agent4-test-2",
    }
    assert report.environment["workspace"] == "clean_sha_verified_checkout"
    assert report.environment["network"] == "enabled"
    assert not any(item.get("workspace_integrity_violations") for item in report.regression_results)
    assert response.model == "scripted-acceptance"
    assert "verification_method" in provider.systems[0]
    assert provider.users[0]["observation"]["acceptance_checklist"][0]["id"] == "AC-1"
    assert provider.image_calls == [[reference], [reference], [reference]]


@pytest.mark.asyncio
async def test_acceptance_agent_preserves_its_submitted_pass_report(tmp_path: Path) -> None:
    repository = tmp_path / "repo-1"
    repository.mkdir()
    (repository / "value.py").write_text("VALUE = 1\n")
    provider = ScriptedProvider(
        [
            {
                "action": "run_command",
                "argv": ["python", "-c", "from pathlib import Path; assert 'VALUE = 1' in Path('value.py').read_text()"],
                "cwd": "repo-1",
                "criterion_ids": ["AC-1"],
            },
            {
                "action": "finish",
                "report": {
                    "approved": True,
                    "summary": "Incomplete report",
                    "criteria": [
                        {
                            "criterion_id": "AC-1",
                            "status": "passed",
                            "summary": "Checked value",
                            "evidence_paths": ["agent4-test-1"],
                        }
                    ],
                    "regression_results": [],
                    "environment": {},
                },
            },
            {"action": "read_file", "path": "repo-1/value.py", "criterion_ids": ["AC-2"]},
            {
                "action": "finish",
                "report": {
                    "approved": True,
                    "summary": "Complete report",
                    "criteria": [
                        {
                            "criterion_id": "AC-1",
                            "status": "passed",
                            "summary": "Checked value",
                            "evidence_paths": ["agent4-test-1"],
                        },
                        {
                            "criterion_id": "AC-2",
                            "status": "passed",
                            "summary": "Inspected source",
                            "evidence_paths": ["agent4-read-2"],
                        },
                    ],
                    "regression_results": [],
                    "environment": {},
                },
            },
        ]
    )

    report, _ = await AcceptanceToolLoop(provider).run(acceptance_context(tmp_path))

    assert report.approved is True
    assert len(provider.users) == 2
    assert [item.criterion_id for item in report.criteria] == ["AC-1"]
    assert report.summary == "Incomplete report"


@pytest.mark.asyncio
async def test_acceptance_agent_preserves_its_rejection_for_a_blocked_should_criterion(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repo-1"
    repository.mkdir()
    (repository / "value.py").write_text("VALUE = 1\n")
    criteria = [
        {
            "id": "AC-must",
            "description": "Local behavior works",
            "verification_method": "Run a local assertion",
            "priority": "must",
        },
        {
            "id": "AC-should",
            "description": "External status is visible",
            "verification_method": "Inspect an unavailable external service",
            "priority": "should",
        },
    ]
    rejected = {
        "approved": False,
        "summary": "Local must passed; external should is unavailable.",
        "criteria": [
            {
                "criterion_id": "AC-must",
                "status": "passed",
                "summary": "Assertion passed.",
                "evidence_paths": ["agent4-test-1"],
            },
            {
                "criterion_id": "AC-should",
                "status": "blocked",
                "summary": "External service is unavailable in the sandbox.",
                "evidence_paths": [],
            },
        ],
        "regression_results": [],
        "environment": {},
    }
    provider = ScriptedProvider(
        [
            {
                "action": "run_command",
                "argv": ["python", "-c", "assert 1 + 1 == 2"],
                "cwd": "repo-1",
                "criterion_ids": ["AC-must"],
            },
            {"action": "finish", "report": rejected},
        ]
    )

    report, _ = await AcceptanceToolLoop(provider).run(acceptance_context(tmp_path, criteria))

    assert report.approved is False
    assert len(provider.users) == 2
    assert report.summary == "Local must passed; external should is unavailable."


@pytest.mark.asyncio
async def test_acceptance_agent_preserves_its_evidence_links(tmp_path: Path) -> None:
    repository = tmp_path / "repo-1"
    repository.mkdir()
    (repository / "value.py").write_text("VALUE = 1\n")
    provider = ScriptedProvider(
        [
            {
                "action": "run_command",
                "argv": ["python", "-c", "assert 1 + 1 == 2"],
                "cwd": "repo-1",
                "criterion_ids": ["AC-1", "AC-2"],
            },
            {
                "action": "finish",
                "report": {
                    "approved": True,
                    "summary": "The checks passed, but evidence ids were copied incorrectly.",
                    "criteria": [
                        {
                            "criterion_id": "AC-1",
                            "status": "passed",
                            "summary": "Checked independently.",
                            "evidence_paths": ["hallucinated-id"],
                        },
                        {
                            "criterion_id": "AC-2",
                            "status": "passed",
                            "summary": "Checked independently.",
                            "evidence_paths": [],
                        },
                    ],
                    "regression_results": [],
                    "environment": {},
                },
            },
        ]
    )

    report, _ = await AcceptanceToolLoop(provider).run(acceptance_context(tmp_path))

    assert report.approved is True
    assert report.criteria[0].evidence_paths == ["hallucinated-id"]
    assert report.criteria[1].evidence_paths == []


@pytest.mark.asyncio
async def test_acceptance_agent_does_not_fall_back_to_a_platform_report(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repo-1"
    repository.mkdir()
    (repository / "value.py").write_text("VALUE = 1\n")
    invalid_report = {
        "approved": False,
        "summary": "The model omitted the confirmed acceptance criteria.",
        "criteria": [],
        "regression_results": [],
        "environment": {},
    }
    provider = ScriptedProvider(
        [
            {
                "action": "run_command",
                "argv": ["python", "-c", "assert 1 + 1 == 2"],
                "cwd": "repo-1",
                "criterion_ids": ["AC-1", "AC-2"],
            },
            {"action": "finish", "report": invalid_report},
            {"action": "finish", "report": invalid_report},
        ]
    )

    report, _ = await AcceptanceToolLoop(provider).run(acceptance_context(tmp_path))

    assert report.approved is False
    assert len(provider.users) == 2
    assert report.summary == "The model omitted the confirmed acceptance criteria."
    assert report.criteria == []


@pytest.mark.asyncio
async def test_acceptance_agent_can_continue_after_a_command_changes_the_disposable_workspace(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repo-1"
    repository.mkdir()
    source = repository / "value.py"
    source.write_text("VALUE = 1\n")
    criteria = [
        {
            "id": "AC-1",
            "description": "Value remains one",
            "verification_method": "Assert the source value",
            "priority": "must",
        }
    ]
    provider = ScriptedProvider(
        [
            {
                "action": "run_command",
                "argv": [
                    "python",
                    "-c",
                    "from pathlib import Path; Path('value.py').write_text('VALUE = 2\\n'); assert True",
                ],
                "cwd": "repo-1",
                "criterion_ids": ["AC-1"],
            },
            {
                "action": "run_command",
                "argv": [
                    "python",
                    "-c",
                    "from pathlib import Path; "
                    "assert Path('value.py').read_text() == 'VALUE = 2\\n'",
                ],
                "cwd": "repo-1",
                "criterion_ids": ["AC-1"],
            },
            {
                "action": "finish",
                "report": {
                    "approved": True,
                    "summary": "Agent completed validation in its disposable workspace.",
                    "criteria": [
                        {
                            "criterion_id": "AC-1",
                            "status": "passed",
                            "summary": "Follow-up assertion passed.",
                            "evidence_paths": ["agent4-test-2"],
                        }
                    ],
                    "regression_results": [],
                    "environment": {},
                },
            },
        ]
    )

    report, _ = await AcceptanceToolLoop(provider).run(acceptance_context(tmp_path, criteria))

    assert report.approved is True
    assert [item["status"] for item in report.regression_results] == ["passed", "passed"]
    assert source.read_text() == "VALUE = 2\n"


@pytest.mark.asyncio
async def test_acceptance_agent_ignores_generated_test_outputs_and_resolves_single_repo_paths(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repo-1"
    repository.mkdir()
    (repository / "value.py").write_text("VALUE = 1\n")
    criteria = [
        {
            "id": "AC-1",
            "description": "Value remains one",
            "verification_method": "Inspect and test the value",
            "priority": "must",
        }
    ]
    provider = ScriptedProvider(
        [
            {"action": "read_file", "path": "value.py", "criterion_ids": ["AC-1"]},
            {
                "action": "run_command",
                "argv": [
                    "python",
                    "-c",
                    "from pathlib import Path; Path('dist-tests').mkdir(); "
                    "Path('dist-tests/value.js').write_text('generated'); assert Path('value.py').exists()",
                ],
                "cwd": "repo-1",
                "criterion_ids": ["AC-1"],
            },
            {
                "action": "finish",
                "report": {
                    "approved": True,
                    "summary": "Source and generated-test behavior verified.",
                    "criteria": [
                        {
                            "criterion_id": "AC-1",
                            "status": "passed",
                            "summary": "Source inspection and assertion passed.",
                            "evidence_paths": ["agent4-read-1", "agent4-test-2"],
                        }
                    ],
                    "regression_results": [],
                    "environment": {},
                },
            },
        ]
    )

    report, _ = await AcceptanceToolLoop(provider).run(acceptance_context(tmp_path, criteria))

    assert report.approved is True
    assert report.regression_results[0]["path"] == "repo-1/value.py"
    assert report.regression_results[1]["status"] == "passed"
    assert "workspace_integrity_violations" not in report.regression_results[1]


@pytest.mark.asyncio
async def test_fake_provider_can_drive_the_acceptance_tool_protocol(tmp_path: Path) -> None:
    repository = tmp_path / "repo-1"
    repository.mkdir()
    (repository / "README.md").write_text("ready\n")
    criteria = [
        {
            "id": "AC-1",
            "description": "Core flow works",
            "verification_method": "Run an assertion",
            "priority": "must",
        }
    ]

    report, response = await AcceptanceToolLoop(FakeLLMProvider()).run(
        acceptance_context(tmp_path, criteria)
    )

    assert report.approved is True
    assert report.criteria[0].evidence_paths == ["agent4-test-1"]
    assert response.model == "fake"
