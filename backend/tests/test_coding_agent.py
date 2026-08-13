import json
from pathlib import Path
import subprocess

import pytest

from app.agents.coding import (
    DeveloperInvalidAction,
    DeveloperStepBudgetExceeded,
    DeveloperToolLoop,
    DeveloperToolStalled,
    DeveloperValidationStalled,
    _diagnostic_excerpt,
)
from app.agents.coding import _is_test_execution_command, _is_validation_command
from app.agents.providers import LLMProvider, ModelResponse


class ScriptedProvider(LLMProvider):
    def __init__(self, actions: list[dict]):
        self.actions = iter(actions)
        self.users: list[dict] = []
        self.systems: list[str] = []

    async def complete(self, system: str, user: str) -> ModelResponse:
        self.systems.append(system)
        self.users.append(json.loads(user))
        return ModelResponse(content=json.dumps(next(self.actions)), prompt_tokens=10, completion_tokens=5, model="scripted")


def test_node_builtin_test_runner_counts_as_test_validation() -> None:
    command = ["node", "--test", "dist-tests/value.test.js"]

    assert _is_validation_command(command) is True
    assert _is_test_execution_command(command) is True
    assert _is_validation_command(["node", "scripts/arbitrary.js"]) is False
    assert _is_test_execution_command(["node", "scripts/arbitrary.js"]) is False


def test_diagnostic_excerpt_prioritizes_tap_failure_details_from_middle() -> None:
    output = "\n".join(
        ["ok 1 - first", *(f"log line {index}" for index in range(200))]
        + [
            "not ok 1085 - selftest input validation",
            "  failureType: 'testCodeFailure'",
            "  error: |-",
            "    undefined pipelines",
            "    + actual - expected",
            "    + 'data.pipelines is empty'",
            "    - 'data.pipelines is undefined'",
        ]
        + [*(f"tail line {index}" for index in range(200)), "# fail 1"]
    )

    excerpt = _diagnostic_excerpt(output, 600)

    assert "not ok 1085" in excerpt
    assert "data.pipelines is empty" in excerpt
    assert "data.pipelines is undefined" in excerpt


@pytest.mark.asyncio
async def test_retry_can_restore_a_missing_tracked_file_before_continuing(tmp_path: Path) -> None:
    repository = tmp_path / "repo-1"
    repository.mkdir()
    subprocess.run(["git", "init"], cwd=repository, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repository, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repository, check=True)
    target = repository / "value.py"
    target.write_text("VALUE = 1\n")
    subprocess.run(["git", "add", "value.py"], cwd=repository, check=True)
    subprocess.run(["git", "commit", "-m", "baseline"], cwd=repository, check=True, capture_output=True)
    target.unlink()
    provider = ScriptedProvider(
        [
            {"action": "restore_file", "path": "repo-1/value.py"},
            {"action": "read_file", "path": "repo-1/value.py"},
            {"action": "replace_text", "path": "repo-1/value.py", "old": "VALUE = 1", "new": "VALUE = 2"},
            {
                "action": "run_command",
                "argv": ["python", "-c", "from value import VALUE; assert VALUE == 2"],
                "cwd": "repo-1",
            },
            {
                "action": "finish",
                "report": {
                    "summary": "Restored the accidentally deleted file and completed the fix.",
                    "repositories_changed": ["repo-1"],
                    "commits": {},
                    "tests": [],
                    "unresolved_risks": [],
                },
            },
        ]
    )
    context = {
        "_previous_attempt_failure": {
            "error_code": "agent.step_budget_exhausted",
            "error_message": "TypeScript error: Cannot find module './value'",
            "changed_paths": ["repo-1/value.py"],
        },
        "artifacts": {
            "workspace_manifest": {
                "workspace_root": str(tmp_path),
                "repositories": [{"repository_id": "repo-1", "relative_path": "repo-1"}],
            }
        },
    }

    report, _ = await DeveloperToolLoop(provider).run(context)

    assert provider.users[0]["progress"]["recoverable_missing_previous_paths"] == ["repo-1/value.py"]
    assert "restore_file" in provider.users[0]["completion_instruction"]
    assert report.files_changed == ["repo-1/value.py"]
    assert target.read_text() == "VALUE = 2\n"


@pytest.mark.asyncio
async def test_developer_agent_modifies_workspace_and_runs_real_test(tmp_path: Path) -> None:
    repository = tmp_path / "repo-1"
    repository.mkdir()
    (repository / "calc.py").write_text("def add(a, b):\n    return 0\n")
    actions = [
        {"action": "read_file", "path": "repo-1/calc.py"},
        {"action": "replace_text", "path": "repo-1/calc.py", "old": "return 0", "new": "return a + b"},
        {"action": "write_file", "path": "repo-1/test_calc.py", "content": "from calc import add\n\ndef test_add():\n    assert add(1, 2) == 3\n"},
        {"action": "run_command", "argv": ["pytest", "-q"], "cwd": "repo-1"},
        {
            "action": "finish",
            "report": {
                "schema_version": "1.0",
                "summary": "Fixed addition and added a regression test.",
                "repositories_changed": ["repo-1"],
                "commits": {},
                "tests": [],
                "unresolved_risks": [],
            },
        },
    ]
    context = {
        "requirement_id": "req-1",
        "title": "Fix addition",
        "description": "Return the sum",
        "artifacts": {
            "workspace_manifest": {
                "workspace_root": str(tmp_path),
                "repositories": [{"repository_id": "repo-1", "relative_path": "repo-1"}],
            }
        },
    }
    provider = ScriptedProvider(actions)
    report, response = await DeveloperToolLoop(provider).run(context)
    assert "a + b" in (repository / "calc.py").read_text()
    assert report.tests[0]["status"] == "passed"
    assert response.model == "scripted"
    assert provider.users[1]["read_evidence"]["repo-1/calc.py"]["head"].startswith("def add")


@pytest.mark.asyncio
async def test_review_rework_can_finish_with_validation_evidence_only(tmp_path: Path) -> None:
    repository = tmp_path / "repo-1"
    repository.mkdir()
    (repository / "value.py").write_text("VALUE = 1\n")
    provider = ScriptedProvider(
        [
            {
                "action": "run_command",
                "argv": ["python", "-c", "from value import VALUE; assert VALUE == 1"],
                "cwd": "repo-1",
            },
            {
                "action": "finish",
                "report": {
                    "summary": "Prior committed change passes the requested validation.",
                    "repositories_changed": [],
                    "commits": {},
                    "tests": [],
                    "unresolved_risks": [],
                },
            },
        ]
    )
    context = {
        "requirement_id": "req-1",
        "title": "Validate prior change",
        "description": "Provide missing test evidence",
        "artifacts": {
            "workspace_manifest": {
                "workspace_root": str(tmp_path),
                "repositories": [{"repository_id": "repo-1", "relative_path": "repo-1"}],
            },
            "development_commit_manifest": {"repositories": [{"repository_id": "repo-1"}]},
            "code_review_report": {
                "approved": False,
                "summary": "Missing validation evidence",
                "findings": [],
            },
        },
    }

    report, _ = await DeveloperToolLoop(provider).run(context)

    assert report.repositories_changed == []
    assert report.tests[0]["status"] == "passed"


@pytest.mark.asyncio
async def test_review_rework_with_high_finding_requires_real_file_change(tmp_path: Path) -> None:
    repository = tmp_path / "repo-1"
    repository.mkdir()
    (repository / "value.py").write_text("VALUE = 1\n")
    provider = ScriptedProvider(
        [
            {
                "action": "run_command",
                "argv": ["python", "-c", "from value import VALUE; assert VALUE == 1"],
                "cwd": "repo-1",
            },
            {
                "action": "finish",
                "report": {
                    "summary": "Validation passes without addressing the review finding.",
                    "repositories_changed": [],
                    "commits": {},
                    "tests": [],
                    "unresolved_risks": [],
                },
            },
            {"action": "read_file", "path": "repo-1/value.py"},
            {"action": "replace_text", "path": "repo-1/value.py", "old": "VALUE = 1", "new": "VALUE = 2"},
            {
                "action": "run_command",
                "argv": ["python", "-c", "from value import VALUE; assert VALUE == 2"],
                "cwd": "repo-1",
            },
            {
                "action": "finish",
                "report": {
                    "summary": "Addressed the mandatory review finding.",
                    "repositories_changed": ["repo-1"],
                    "commits": {},
                    "tests": [],
                    "unresolved_risks": [],
                },
            },
        ]
    )
    context = {
        "requirement_id": "req-1",
        "title": "Address review finding",
        "description": "The review requires a source change",
        "artifacts": {
            "workspace_manifest": {
                "workspace_root": str(tmp_path),
                "repositories": [{"repository_id": "repo-1", "relative_path": "repo-1"}],
            },
            "development_commit_manifest": {"repositories": [{"repository_id": "repo-1"}]},
            "code_review_report": {
                "approved": False,
                "summary": "A source change is required",
                "findings": [
                    {
                        "severity": "high",
                        "required_change": "Replace the unsafe implementation.",
                    }
                ],
            },
        },
    }

    report, _ = await DeveloperToolLoop(provider).run(context)

    assert report.repositories_changed == ["repo-1"]
    assert (repository / "value.py").read_text() == "VALUE = 2\n"
    assert provider.users[2]["observation"]["message"] == "未检测到实际文件修改，不能 finish"


@pytest.mark.asyncio
async def test_retry_inherits_files_changed_by_previous_attempt(tmp_path: Path) -> None:
    repository = tmp_path / "repo-1"
    repository.mkdir()
    (repository / "value.py").write_text("VALUE = 2\n")
    provider = ScriptedProvider(
        [
            {
                "action": "run_command",
                "argv": ["python", "-c", "from value import VALUE; assert VALUE == 2"],
                "cwd": "repo-1",
            },
            {
                "action": "finish",
                "report": {
                    "summary": "Validated the source change left by the previous attempt.",
                    "repositories_changed": ["repo-1"],
                    "commits": {},
                    "tests": [],
                    "unresolved_risks": [],
                },
            },
        ]
    )
    context = {
        "requirement_id": "req-1",
        "title": "Continue an interrupted implementation",
        "description": "Validate and finish the prior attempt's change",
        "_previous_attempt_failure": {
            "error_code": "agent.step_budget_exhausted",
            "error_message": "file_changes=1",
            "changed_paths": ["repo-1/value.py"],
        },
        "artifacts": {
            "workspace_manifest": {
                "workspace_root": str(tmp_path),
                "repositories": [{"repository_id": "repo-1", "relative_path": "repo-1"}],
            },
            "development_commit_manifest": {"repositories": [{"repository_id": "repo-1"}]},
            "code_review_report": {
                "approved": False,
                "summary": "A source correction is mandatory",
                "findings": [
                    {
                        "severity": "high",
                        "required_change": "Correct value.py and validate it.",
                    }
                ],
            },
        },
    }

    report, _ = await DeveloperToolLoop(provider).run(context)

    assert report.repositories_changed == ["repo-1"]
    assert report.files_changed == ["repo-1/value.py"]


@pytest.mark.asyncio
async def test_review_requested_unit_test_requires_test_file_and_test_runner(tmp_path: Path) -> None:
    repository = tmp_path / "repo-1"
    repository.mkdir()
    (repository / "value.py").write_text("VALUE = 1\n")
    provider = ScriptedProvider(
        [
            {"action": "read_file", "path": "repo-1/value.py"},
            {"action": "replace_text", "path": "repo-1/value.py", "old": "VALUE = 1", "new": "VALUE = 2"},
            {
                "action": "run_command",
                "argv": ["python", "-c", "from pathlib import Path; assert 'VALUE = 2' in Path('value.py').read_text()"],
                "cwd": "repo-1",
            },
            {
                "action": "finish",
                "report": {
                    "summary": "Scanned the implementation text.",
                    "repositories_changed": ["repo-1"],
                    "commits": {},
                    "tests": [],
                    "unresolved_risks": [],
                },
            },
            {
                "action": "write_file",
                "path": "repo-1/test_value.py",
                "content": "from value import VALUE\n\ndef test_value():\n    assert VALUE == 2\n",
            },
            {"action": "run_command", "argv": ["pytest", "-q"], "cwd": "repo-1"},
            {
                "action": "finish",
                "report": {
                    "summary": "Added and executed the requested unit test.",
                    "repositories_changed": ["repo-1"],
                    "commits": {},
                    "tests": [],
                    "unresolved_risks": [],
                },
            },
        ]
    )
    context = {
        "requirement_id": "req-1",
        "title": "Add regression coverage",
        "description": "Implement and test the behavior",
        "artifacts": {
            "workspace_manifest": {
                "workspace_root": str(tmp_path),
                "repositories": [{"repository_id": "repo-1", "relative_path": "repo-1"}],
            },
            "development_commit_manifest": {"repositories": [{"repository_id": "repo-1"}]},
            "code_review_report": {
                "approved": False,
                "summary": "Independent coverage is missing",
                "findings": [
                    {
                        "severity": "medium",
                        "required_change": "Add and execute a unit test for the changed behavior.",
                    }
                ],
            },
        },
    }

    report, _ = await DeveloperToolLoop(provider).run(context)

    assert report.files_changed == ["repo-1/test_value.py", "repo-1/value.py"]
    assert provider.users[0]["progress"]["required_test_file_present"] is False
    assert provider.users[0]["progress"]["required_test_command_passed"] is False
    assert "禁止 finish" in provider.users[0]["completion_instruction"]
    assert provider.users[4]["observation"]["message"].startswith("代码审查要求新增或修改测试")
    assert "创建或修改真实测试文件" in provider.users[4]["completion_instruction"]
    assert provider.users[5]["progress"]["required_test_file_present"] is True
    assert provider.users[5]["progress"]["required_test_command_passed"] is False
    assert provider.users[6]["progress"]["required_test_command_passed"] is True
    assert report.tests[-1]["command"] == ["pytest", "-q"]


@pytest.mark.asyncio
async def test_developer_agent_exhaustion_reports_progress_and_recent_actions(tmp_path: Path) -> None:
    repository = tmp_path / "repo-1"
    repository.mkdir()
    (repository / "AGENTS.md").write_text("# Agents\n")
    context = {
        "requirement_id": "req-1",
        "title": "Organize agent guide",
        "description": "Update AGENTS.md",
        "artifacts": {
            "workspace_manifest": {
                "workspace_root": str(tmp_path),
                "repositories": [{"repository_id": "repo-1", "relative_path": "repo-1"}],
            }
        },
    }

    with pytest.raises(DeveloperStepBudgetExceeded) as raised:
        await DeveloperToolLoop(
            ScriptedProvider([
                {"action": "read_file", "path": "repo-1/AGENTS.md"},
                {"action": "read_file", "path": "repo-1/AGENTS.md"},
            ]),
            max_steps=2,
        ).run(context)

    assert raised.value.code == "agent.step_budget_exhausted"
    assert raised.value.token_usage == 30
    assert raised.value.retryable is False
    assert "file_changes=0" in str(raised.value)
    assert "read_file(repo-1/AGENTS.md)" in str(raised.value)


@pytest.mark.asyncio
async def test_developer_agent_gets_finish_only_grace_after_last_step_test(tmp_path: Path) -> None:
    repository = tmp_path / "repo-1"
    repository.mkdir()
    (repository / "value.py").write_text("VALUE = 1\n")
    provider = ScriptedProvider(
        [
            {
                "action": "replace_text",
                "path": "repo-1/value.py",
                "old": "VALUE = 1",
                "new": "VALUE = 2",
            },
            {
                "action": "run_command",
                "argv": ["python", "-c", "from value import VALUE; assert VALUE == 2"],
                "cwd": "repo-1",
            },
            {
                "action": "finish",
                "report": {
                    "summary": "Validated on the final normal tool step.",
                    "repositories_changed": ["repo-1"],
                    "commits": {},
                    "tests": [],
                    "unresolved_risks": [],
                },
            },
        ]
    )
    context = {
        "artifacts": {
            "workspace_manifest": {
                "workspace_root": str(tmp_path),
                "repositories": [{"repository_id": "repo-1", "relative_path": "repo-1"}],
            }
        }
    }

    report, _ = await DeveloperToolLoop(provider, max_steps=2).run(context)

    assert len(provider.users) == 3
    assert provider.users[2]["steps_remaining"] == 0
    assert "收尾槽" in provider.users[2]["completion_instruction"]
    assert report.files_changed == ["repo-1/value.py"]
    assert report.tests[0]["status"] == "passed"


@pytest.mark.asyncio
async def test_developer_agent_can_continue_after_budget_exhaustion_with_workspace_changes(tmp_path: Path) -> None:
    repository = tmp_path / "repo-1"
    repository.mkdir()
    (repository / "implementation.py").write_text("VALUE = 1\n")
    context = {
        "artifacts": {
            "workspace_manifest": {
                "workspace_root": str(tmp_path),
                "repositories": [{"repository_id": "repo-1", "relative_path": "repo-1"}],
            }
        }
    }

    with pytest.raises(DeveloperStepBudgetExceeded) as raised:
        await DeveloperToolLoop(
            ScriptedProvider([
                {
                    "action": "replace_text",
                    "path": "repo-1/implementation.py",
                    "old": "VALUE = 1",
                    "new": "VALUE = 2",
                }
            ]),
            max_steps=1,
        ).run(context)

    assert raised.value.retryable is True
    assert "file_changes=1" in str(raised.value)


@pytest.mark.asyncio
async def test_developer_agent_invalid_actions_have_distinct_diagnostic(tmp_path: Path) -> None:
    repository = tmp_path / "repo-1"
    repository.mkdir()
    context = {
        "artifacts": {
            "workspace_manifest": {
                "workspace_root": str(tmp_path),
                "repositories": [{"repository_id": "repo-1", "relative_path": "repo-1"}],
            }
        }
    }

    with pytest.raises(DeveloperInvalidAction) as raised:
        await DeveloperToolLoop(
            ScriptedProvider([{"schema_version": "1.0"}, {"summary": "not an action"}]),
            max_steps=2,
        ).run(context)

    assert raised.value.code == "agent.invalid_action"
    assert raised.value.token_usage == 30
    assert "invalid_outputs=2" in str(raised.value)


@pytest.mark.asyncio
async def test_developer_agent_repairs_missing_python_executable(tmp_path: Path) -> None:
    repository = tmp_path / "repo-1"
    repository.mkdir()
    (repository / "AGENTS.md").write_text("# Agents\n")
    actions = [
        {"action": "write_file", "path": "repo-1/AGENTS.md", "content": "# Agents\n\n## Data flow\n"},
        {
            "action": "run_command",
            "argv": ["-c", "from pathlib import Path; assert 'Data flow' in Path('AGENTS.md').read_text()"],
            "cwd": "repo-1",
        },
        {
            "action": "finish",
            "report": {
                "summary": "Updated the guide.",
                "repositories_changed": ["repo-1"],
                "commits": {},
                "tests": [],
                "unresolved_risks": [],
            },
        },
    ]
    context = {
        "artifacts": {
            "workspace_manifest": {
                "workspace_root": str(tmp_path),
                "repositories": [{"repository_id": "repo-1", "relative_path": "repo-1"}],
            }
        }
    }

    report, _ = await DeveloperToolLoop(ScriptedProvider(actions)).run(context)

    assert report.tests[0]["command"][0] == "python"
    assert report.tests[0]["status"] == "passed"


@pytest.mark.asyncio
async def test_developer_agent_hard_blocks_repeated_read_only_actions(tmp_path: Path) -> None:
    repository = tmp_path / "repo-1"
    repository.mkdir()
    (repository / "AGENTS.md").write_text("# Agents\n")
    provider = ScriptedProvider([
        {"action": "read_file", "path": "repo-1/AGENTS.md"},
        {"action": "read_file", "path": "repo-1/AGENTS.md"},
        {"action": "read_file", "path": "repo-1/AGENTS.md"},
        {"action": "write_file", "path": "repo-1/AGENTS.md", "content": "# Agents\n\n## Architecture\n"},
        {"action": "run_command", "argv": ["python", "-c", "from pathlib import Path; assert 'Architecture' in Path('AGENTS.md').read_text()"], "cwd": "repo-1"},
        {
            "action": "finish",
            "report": {
                "summary": "Updated the guide.",
                "repositories_changed": ["repo-1"],
                "commits": {},
                "tests": [],
                "unresolved_risks": [],
            },
        },
    ])
    context = {
        "description": "x" * 50_000,
        "conversation": [{"author_type": "user", "stage": "blocked", "body": "remove duplicate sections"}],
        "artifacts": {
            "workspace_manifest": {
                "workspace_root": str(tmp_path),
                "repositories": [{"repository_id": "repo-1", "relative_path": "repo-1"}],
            },
            "clarification_spec": {"summary": "short", "ignored_large_field": "y" * 50_000},
        },
    }

    await DeveloperToolLoop(provider).run(context)

    assert provider.users[3]["observation"]["type"] == "action_blocked"
    assert "ignored_large_field" not in json.dumps(provider.users[0], ensure_ascii=False)
    assert len(provider.users[0]["context"]["description"]) == 12_000
    assert "只能使用仓库相对路径" in provider.systems[0]
    assert "不得记录 workspace_root、repository_id" in provider.systems[0]
    assert "已存在的内容不得" in provider.systems[0]
    assert "禁止反复运行等价的失败脚本" in provider.systems[0]
    assert provider.users[0]["context"]["conversation"][0]["body"] == "remove duplicate sections"


@pytest.mark.asyncio
async def test_developer_agent_still_blocks_repeated_reads_after_a_mutation(tmp_path: Path) -> None:
    repository = tmp_path / "repo-1"
    repository.mkdir()
    (repository / "implementation.py").write_text("VALUE = 1\n")
    provider = ScriptedProvider([
        {
            "action": "replace_text",
            "path": "repo-1/implementation.py",
            "old": "VALUE = 1",
            "new": "VALUE = 2",
        },
        {"action": "read_file", "path": "repo-1/implementation.py"},
        {"action": "read_file", "path": "repo-1/implementation.py"},
        {"action": "read_file", "path": "repo-1/implementation.py"},
        {
            "action": "run_command",
            "argv": ["python", "-c", "from implementation import VALUE; assert VALUE == 2"],
            "cwd": "repo-1",
        },
        {
            "action": "finish",
            "report": {
                "summary": "Updated and verified the implementation.",
                "repositories_changed": ["repo-1"],
                "commits": {},
                "tests": [],
                "unresolved_risks": [],
            },
        },
    ])
    context = {
        "artifacts": {
            "workspace_manifest": {
                "workspace_root": str(tmp_path),
                "repositories": [{"repository_id": "repo-1", "relative_path": "repo-1"}],
            }
        }
    }

    await DeveloperToolLoop(provider).run(context)

    assert provider.users[4]["observation"]["type"] == "action_blocked"
    assert "重复只读操作" in provider.users[4]["observation"]["message"]


@pytest.mark.asyncio
async def test_developer_agent_blocks_destructive_large_file_rewrite(tmp_path: Path) -> None:
    repository = tmp_path / "repo-1"
    repository.mkdir()
    original = "# Agents\n" + ("- durable rule\n" * 700)
    (repository / "AGENTS.md").write_text(original)
    provider = ScriptedProvider([
        {"action": "write_file", "path": "repo-1/AGENTS.md", "content": "# Agents\n\n## Architecture\n"},
        {"action": "replace_text", "path": "repo-1/AGENTS.md", "old": original, "new": "# Agents\n"},
        {
            "action": "replace_text",
            "path": "repo-1/AGENTS.md",
            "old": "# Agents\n",
            "new": "# Agents\n\n## Architecture\n\nPreserved guide.\n",
        },
        {"action": "run_command", "argv": ["python", "-c", "from pathlib import Path; assert 'Architecture' in Path('AGENTS.md').read_text()"], "cwd": "repo-1"},
        {
            "action": "finish",
            "report": {
                "summary": "Added architecture guidance without deleting durable rules.",
                "repositories_changed": ["repo-1"],
                "commits": {},
                "tests": [],
                "unresolved_risks": [],
            },
        },
    ])
    context = {
        "artifacts": {
            "workspace_manifest": {
                "workspace_root": str(tmp_path),
                "repositories": [{"repository_id": "repo-1", "relative_path": "repo-1"}],
            }
        }
    }

    await DeveloperToolLoop(provider).run(context)

    assert provider.users[1]["observation"]["type"] == "tool_error"
    assert "destructive whole-file rewrite" in provider.users[1]["observation"]["message"]
    assert provider.users[2]["observation"]["type"] == "tool_error"
    assert "destructive large replacement" in provider.users[2]["observation"]["message"]
    updated = (repository / "AGENTS.md").read_text()
    assert "## Architecture" in updated
    assert updated.count("durable rule") == 700


@pytest.mark.asyncio
async def test_developer_agent_blocks_broad_same_size_rewrite_of_large_file(tmp_path: Path) -> None:
    repository = tmp_path / "repo-1"
    repository.mkdir()
    original = "\n".join(f"original line {index}" for index in range(700))
    replacement = "\n".join(f"invented line {index}" for index in range(700))
    (repository / "implementation.py").write_text(original)
    provider = ScriptedProvider([
        {"action": "write_file", "path": "repo-1/implementation.py", "content": replacement},
        {
            "action": "replace_text",
            "path": "repo-1/implementation.py",
            "old": "original line 10\noriginal line 11",
            "new": "original line 10 # focused fix\noriginal line 11",
        },
        {
            "action": "run_command",
            "argv": ["python", "-c", "from pathlib import Path; assert 'focused fix' in Path('implementation.py').read_text()"],
            "cwd": "repo-1",
        },
        {
            "action": "finish",
            "report": {
                "summary": "Applied a focused change.",
                "repositories_changed": ["repo-1"],
                "commits": {},
                "tests": [],
                "unresolved_risks": [],
            },
        },
    ])
    context = {
        "artifacts": {
            "workspace_manifest": {
                "workspace_root": str(tmp_path),
                "repositories": [{"repository_id": "repo-1", "relative_path": "repo-1"}],
            }
        }
    }

    await DeveloperToolLoop(provider).run(context)

    assert provider.users[1]["observation"]["type"] == "tool_error"
    assert "broad whole-file rewrite" in provider.users[1]["observation"]["message"]
    assert "invented line" not in (repository / "implementation.py").read_text()


@pytest.mark.asyncio
async def test_developer_agent_allows_reviewable_rewrite_named_by_confirmed_architecture(tmp_path: Path) -> None:
    repository = tmp_path / "repo-1"
    repository.mkdir()
    original = "\n".join(f"obsolete pipeline line {index}" for index in range(700))
    replacement = "\n".join(f"workflow capability line {index}" for index in range(120))
    (repository / "implementation.py").write_text(original)
    provider = ScriptedProvider([
        {"action": "read_file", "path": "repo-1/implementation.py"},
        {
            "action": "write_file",
            "path": "repo-1/implementation.py",
            "content": replacement,
            "rewrite_reason": "The confirmed architecture explicitly requires migrating implementation.py from the obsolete pipeline to the workflow capability.",
        },
        {
            "action": "run_command",
            "argv": ["python", "-c", "from pathlib import Path; text=Path('implementation.py').read_text(); assert 'workflow capability' in text and 'obsolete pipeline' not in text"],
            "cwd": "repo-1",
        },
        {
            "action": "finish",
            "report": {
                "summary": "Migrated the architecture-confirmed implementation target.",
                "repositories_changed": ["repo-1"],
                "commits": {},
                "tests": [],
                "unresolved_risks": [],
            },
        },
    ])
    context = {
        "artifacts": {
            "workspace_manifest": {
                "workspace_root": str(tmp_path),
                "repositories": [{"repository_id": "repo-1", "relative_path": "repo-1"}],
            },
            "architecture_revision": {
                "repositories": [{
                    "repository_id": "repo-1",
                    "changes": ["Migrate implementation.py to the workflow capability."],
                    "test_commands": [
                        "python -c \"from pathlib import Path; assert 'workflow capability' in Path('implementation.py').read_text()\""
                    ],
                }],
            },
        }
    }

    await DeveloperToolLoop(provider).run(context)

    assert provider.users[2]["observation"]["type"] == "mutation_with_validation"
    assert provider.users[2]["observation"]["mutation"]["controlled_broad_rewrite"] is True
    assert provider.users[2]["observation"]["automatic_validation"]["returncode"] == 0
    assert (repository / "implementation.py").read_text() == replacement


@pytest.mark.asyncio
async def test_developer_agent_stops_repeating_the_same_tool_error(tmp_path: Path) -> None:
    repository = tmp_path / "repo-1"
    repository.mkdir()
    context = {
        "artifacts": {
            "workspace_manifest": {
                "workspace_root": str(tmp_path),
                "repositories": [{"repository_id": "repo-1", "relative_path": "repo-1"}],
            }
        }
    }

    with pytest.raises(DeveloperToolStalled) as raised:
        await DeveloperToolLoop(
            ScriptedProvider([
                {"action": "run_command", "argv": ["npx", "create-react-app", "demo"], "cwd": "repo-1"},
                {"action": "run_command", "argv": ["npx", "create-react-app", "demo"], "cwd": "repo-1"},
                {"action": "run_command", "argv": ["npx", "create-react-app", "demo"], "cwd": "repo-1"},
            ])
        ).run(context)

    assert raised.value.code == "agent.tool_stalled"
    assert raised.value.token_usage == 45
    assert "repeated_errors=3" in str(raised.value)
    assert "npx tool is not allowlisted" in str(raised.value)


@pytest.mark.asyncio
async def test_developer_agent_allows_distinct_recoverable_edit_errors(tmp_path: Path) -> None:
    repository = tmp_path / "repo-1"
    repository.mkdir()
    (repository / "implementation.py").write_text("VALUE = 1\n")
    actions = [
        {
            "action": "replace_text",
            "path": "repo-1/implementation.py",
            "old": f"missing-{index}",
            "new": "unused",
        }
        for index in range(7)
    ]
    actions.extend([
        {
            "action": "replace_text",
            "path": "repo-1/implementation.py",
            "old": "VALUE = 1",
            "new": "VALUE = 2",
        },
        {
            "action": "run_command",
            "argv": ["python", "-c", "from implementation import VALUE; assert VALUE == 2"],
            "cwd": "repo-1",
        },
        {
            "action": "finish",
            "report": {
                "summary": "Recovered from distinct edit misses and verified the change.",
                "repositories_changed": ["repo-1"],
                "commits": {},
                "tests": [],
                "unresolved_risks": [],
            },
        },
    ])
    context = {
        "artifacts": {
            "workspace_manifest": {
                "workspace_root": str(tmp_path),
                "repositories": [{"repository_id": "repo-1", "relative_path": "repo-1"}],
            }
        }
    }

    report, _ = await DeveloperToolLoop(ScriptedProvider(actions)).run(context)

    assert report.tests[0]["status"] == "passed"
    assert (repository / "implementation.py").read_text() == "VALUE = 2\n"


@pytest.mark.asyncio
async def test_developer_agent_stops_repeating_an_identical_validation_failure(tmp_path: Path) -> None:
    repository = tmp_path / "repo-1"
    repository.mkdir()
    failing_action = {
        "action": "run_command",
        "argv": ["python", "-c", "import sys; print('same compiler error'); sys.exit(1)"],
        "cwd": "repo-1",
    }
    context = {
        "artifacts": {
            "workspace_manifest": {
                "workspace_root": str(tmp_path),
                "repositories": [{"repository_id": "repo-1", "relative_path": "repo-1"}],
            }
        }
    }

    with pytest.raises(DeveloperValidationStalled) as raised:
        await DeveloperToolLoop(
            ScriptedProvider([failing_action, failing_action, failing_action])
        ).run(context)

    assert raised.value.code == "agent.validation_stalled"
    assert raised.value.token_usage == 45
    assert "repeated_failure=3" in str(raised.value)
    assert "same compiler error" in str(raised.value)


def test_build_package_and_smoke_scripts_count_as_validation_commands() -> None:
    from app.agents.coding import _is_validation_command

    assert _is_validation_command(["npm", "run", "build:scripts"])
    assert _is_validation_command(["pnpm", "run", "package:cli"])
    assert _is_validation_command(["yarn", "smoke:cli"])
    assert _is_validation_command(["npx", "tsc", "--noEmit"])


@pytest.mark.asyncio
async def test_developer_agent_can_recover_with_bounded_line_edit_after_non_unique_text(tmp_path: Path) -> None:
    repository = tmp_path / "repo-1"
    repository.mkdir()
    (repository / "settings.py").write_text("MODE = 'old'\nMODE = 'old'\nRESULT = 0\n")
    provider = ScriptedProvider([
        {
            "action": "replace_text",
            "path": "repo-1/settings.py",
            "old": "MODE = 'old'",
            "new": "MODE = 'new'",
        },
        {
            "action": "read_lines",
            "path": "repo-1/settings.py",
            "start_line": 1,
            "end_line": 3,
        },
        {
            "action": "replace_lines",
            "path": "repo-1/settings.py",
            "start_line": 2,
            "end_line": 3,
            "content": "MODE = 'new'\nRESULT = 1",
        },
        {
            "action": "run_command",
            "argv": ["python", "-c", "from pathlib import Path; text=Path('settings.py').read_text(); assert text.count(\"MODE = 'new'\") == 1 and 'RESULT = 1' in text"],
            "cwd": "repo-1",
        },
        {
            "action": "finish",
            "report": {
                "summary": "Updated the selected configuration lines.",
                "repositories_changed": ["repo-1"],
                "commits": {},
                "tests": [],
                "unresolved_risks": [],
            },
        },
    ])
    context = {
        "artifacts": {
            "workspace_manifest": {
                "workspace_root": str(tmp_path),
                "repositories": [{"repository_id": "repo-1", "relative_path": "repo-1"}],
            }
        }
    }

    await DeveloperToolLoop(provider).run(context)

    assert provider.users[1]["observation"]["type"] == "tool_error"
    assert "matched 2 times" in provider.users[1]["observation"]["message"]
    assert provider.users[2]["observation"]["type"] == "file_lines"
    assert provider.users[2]["observation"]["lines"][1] == {"line": 2, "text": "MODE = 'old'"}
    assert (repository / "settings.py").read_text() == "MODE = 'old'\nMODE = 'new'\nRESULT = 1\n"


@pytest.mark.asyncio
async def test_developer_agent_does_not_count_print_only_command_as_test(tmp_path: Path) -> None:
    repository = tmp_path / "repo-1"
    repository.mkdir()
    (repository / "AGENTS.md").write_text("# Agents\n")
    provider = ScriptedProvider([
        {"action": "write_file", "path": "repo-1/AGENTS.md", "content": "# Agents\n\n## Architecture\n"},
        {"action": "run_command", "argv": ["python", "-c", "print(open('AGENTS.md').read())"], "cwd": "repo-1"},
        {
            "action": "finish",
            "report": {
                "summary": "Not actually verified.",
                "repositories_changed": ["repo-1"],
                "commits": {},
                "tests": [],
                "unresolved_risks": [],
            },
        },
        {"action": "run_command", "argv": ["python", "-c", "from pathlib import Path; assert 'Architecture' in Path('AGENTS.md').read_text()"], "cwd": "repo-1"},
        {
            "action": "finish",
            "report": {
                "summary": "Verified update.",
                "repositories_changed": ["repo-1"],
                "commits": {},
                "tests": [],
                "unresolved_risks": [],
            },
        },
    ])
    context = {
        "artifacts": {
            "workspace_manifest": {
                "workspace_root": str(tmp_path),
                "repositories": [{"repository_id": "repo-1", "relative_path": "repo-1"}],
            }
        }
    }

    report, _ = await DeveloperToolLoop(provider).run(context)

    assert provider.users[2]["observation"]["type"] == "action_blocked"
    assert "read_file" in provider.users[2]["observation"]["message"]
    assert provider.users[3]["observation"]["message"] == "finish 前必须至少运行一个成功的测试命令"
    assert len(report.tests) == 1
    assert "assert" in report.tests[0]["command"][2]


@pytest.mark.asyncio
async def test_developer_agent_rejects_empty_or_duplicate_markdown_sections(tmp_path: Path) -> None:
    repository = tmp_path / "repo-1"
    repository.mkdir()
    provider = ScriptedProvider([
        {"action": "write_file", "path": "repo-1/GUIDE.md", "content": "# Guide\n\n## Empty\n\n## Real\n\nbody\n\n## Real\n\nmore\n"},
        {"action": "run_command", "argv": ["python", "-c", "from pathlib import Path; assert Path('GUIDE.md').exists()"], "cwd": "repo-1"},
        {"action": "finish", "report": {"summary": "bad", "repositories_changed": ["repo-1"], "commits": {}, "tests": [], "unresolved_risks": []}},
        {"action": "write_file", "path": "repo-1/GUIDE.md", "content": "# Guide\n\n## Empty\n\ncontent\n\n## Real\n\nbody\n"},
        {"action": "run_command", "argv": ["python", "-c", "from pathlib import Path; s=Path('GUIDE.md').read_text(); assert s.count('## Real') == 1"], "cwd": "repo-1"},
        {"action": "finish", "report": {"summary": "fixed", "repositories_changed": ["repo-1"], "commits": {}, "tests": [], "unresolved_risks": []}},
    ])
    context = {"artifacts": {"workspace_manifest": {"workspace_root": str(tmp_path), "repositories": [{"repository_id": "repo-1", "relative_path": "repo-1"}]}}}

    await DeveloperToolLoop(provider).run(context)

    assert provider.users[3]["observation"]["message"] == "Markdown 质量门禁未通过，修复后才能 finish"
    assert any("duplicate heading" in item for item in provider.users[3]["observation"]["issues"])
    assert any("empty heading" in item for item in provider.users[3]["observation"]["issues"])
