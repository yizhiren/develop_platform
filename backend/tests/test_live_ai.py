import os
import subprocess
from pathlib import Path

import pytest

from app.agents.acceptance import AcceptanceToolLoop
from app.agents.clarification import PiClarificationToolLoop
from app.agents.pi_bridge import PiAgentCoreBridge
from app.agents.providers import OpenAICompatibleProvider
from app.agents.coding import DeveloperToolLoop
from app.agents.runtime import AgentRuntime, ROLE_SCHEMAS
from app.core.config import Settings
from app.providers.git import GitProvider, PullRequestRef
from app.services.git_workspace import GitWorkspaceManager
from app.worker import build_runtimes


pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.live_ai,
    pytest.mark.skipif(
        os.getenv("RUN_LIVE_AI_TESTS") != "1",
        reason="set RUN_LIVE_AI_TESTS=1 to send paid model requests",
    ),
]


@pytest.mark.parametrize("role", ["clarify", "architect", "develop", "review", "accept"])
async def test_deepseek_agent_protocols(role: str) -> None:
    provider = OpenAICompatibleProvider(
        os.getenv("LLM_BASE_URL", "https://api.deepseek.com"),
        os.environ["DEEPSEEK_API_KEY"],
        os.getenv("LLM_MODEL", "deepseek-v4-flash"),
        timeout=float(os.getenv("LIVE_AI_TIMEOUT_SECONDS", "60")),
        max_tokens=int(os.getenv("LIVE_AI_MAX_TOKENS", "1500")),
    )
    runtime = AgentRuntime(provider)
    output, response = await runtime.run(
        role,
        {
            "title": "为项目增加健康检查接口",
            "description": "返回服务、SQLite 与 Redis 状态，并为核心行为提供自动化测试。",
            "test_mode": True,
            "untrusted_repository_context": "No repository content is included in this protocol test.",
        },
    )
    assert isinstance(output, ROLE_SCHEMAS[role])
    assert response.model


@pytest.mark.parametrize(
    ("agent_key", "role"),
    [
        ("agent1", "clarify"),
        ("agent2", "architect"),
        ("agent3", "develop"),
        ("agent4", "accept"),
    ],
)
async def test_each_configured_agent_profile_reaches_its_model(agent_key: str, role: str) -> None:
    runtime = build_runtimes()[agent_key]
    output, response = await runtime.run(
        role,
        {
            "title": "验证独立 Agent 模型配置",
            "description": "只验证当前角色能够使用自己的模型配置返回结构化 JSON。",
            "test_mode": True,
        },
    )
    assert isinstance(output, ROLE_SCHEMAS[role])
    assert response.model


async def test_deepseek_pi_clarifier_reads_repository_before_asking_questions(
    tmp_path: Path,
) -> None:
    runtime = build_runtimes()["agent1"]
    assert isinstance(runtime.provider, OpenAICompatibleProvider)
    workspace_root = tmp_path / "workspaces"
    analysis_root = workspace_root / "live-pi-analysis"
    repository = analysis_root / "repo-1"
    repository.mkdir(parents=True)
    (repository / "package.json").write_text(
        '{"scripts":{"test":"node --test"},"engines":{"node":">=22"}}'
    )
    (repository / ".github" / "workflows").mkdir(parents=True)
    (repository / ".github" / "workflows" / "ci.yml").write_text(
        "jobs:\n  test:\n    steps:\n      - run: npm test\n"
    )
    context = {
        "requirement_id": "live-pi-clarifier-1",
        "title": "修复 CI 测试步骤",
        "description": (
            "CI 必须使用仓库现有测试脚本并在 Node 22 下通过。技术事实从仓库读取；"
            "若这些信息已充分，不要向用户提问。"
        ),
        "repositories": [{"repository_id": "repo-1", "full_name": "local/pi-test"}],
        "artifacts": {
            "repository_analysis": {
                "schema_version": "1.0",
                "source": "trusted_read_only_checkout",
                "workspace_root": str(analysis_root),
                "repositories": [
                    {
                        "repository_id": "repo-1",
                        "relative_path": "repo-1",
                        "full_name": "local/pi-test",
                        "head_sha": "test-sha",
                        "file_tree": ["package.json", ".github/workflows/ci.yml"],
                    }
                ],
            }
        },
    }
    bridge = PiAgentCoreBridge(
        Path(os.getenv("PI_AGENT_CORE_BRIDGE_PATH", "/srv/pi-bridge/bridge.mjs")),
        timeout_seconds=int(os.getenv("PI_AGENT_CORE_TIMEOUT_SECONDS", "240")),
    )

    report, response = await PiClarificationToolLoop(
        runtime.provider,
        bridge,
        workspace_root,
        max_turns=8,
    ).run(context)

    assert report.repository_ids == ["repo-1"]
    assert any("npm" in item.lower() or "node" in item.lower() for item in report.dependencies)
    assert response.model
    assert response.prompt_tokens + response.completion_tokens > 0


async def test_deepseek_developer_tool_loop_changes_code_and_runs_tests(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # This standalone test container does not mount the production executor socket.
    monkeypatch.delenv("SANDBOX_EXECUTOR_SOCKET", raising=False)
    source = tmp_path / "source"
    remote = tmp_path / "remote.git"
    source.mkdir()
    _git("init", "-b", "main", cwd=source)
    _git("config", "user.name", "Live Test", cwd=source)
    _git("config", "user.email", "live@example.com", cwd=source)
    (source / "calculator.py").write_text("def add(left, right):\n    return left - right\n")
    (source / "test_calculator.py").write_text(
        "from calculator import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n"
    )
    _git("add", ".", cwd=source)
    _git("commit", "-m", "initial", cwd=source)
    _git("clone", "--bare", str(source), str(remote), cwd=tmp_path)
    manager = GitWorkspaceManager(
        Settings(_env_file=None, workspace_root=tmp_path / "workspaces", allow_local_git=True)
    )
    provider = OpenAICompatibleProvider(
        os.getenv("LLM_BASE_URL", "https://api.deepseek.com"),
        os.environ["DEEPSEEK_API_KEY"],
        os.getenv("LLM_MODEL", "deepseek-v4-flash"),
        timeout=float(os.getenv("LIVE_AI_TIMEOUT_SECONDS", "60")),
        max_tokens=int(os.getenv("LIVE_AI_MAX_TOKENS", "1500")),
    )
    context: dict = {
        "requirement_id": "live-coding-1",
        "title": "修复加法函数",
        "description": "calculator.py 的 add 应返回两数之和。必须修复实现，并运行现有 pytest 测试。",
        "repositories": [{
            "requirement_repository_id": "link-1",
            "repository_id": "repo-1",
            "provider": "github",
            "full_name": "local/calculator",
            "clone_url": remote.as_uri(),
            "target_branch": "main",
        }],
        "artifacts": {
            "architecture_plan": {
                "target_architecture": "只把 calculator.add 的减法改为加法，不改变公开接口。",
                "test_strategy": ["在 repo-1 运行 pytest -q"],
            },
        },
    }
    context["artifacts"]["workspace_manifest"] = manager.prepare(context)
    repository = Path(context["artifacts"]["workspace_manifest"]["workspace_root"]) / "repo-1"
    report, response = await DeveloperToolLoop(provider, max_steps=12).run(context)
    assert "return left + right" in (repository / "calculator.py").read_text()
    assert report.tests and all(item["status"] == "passed" for item in report.tests)
    assert response.model
    context["artifacts"]["development_report"] = report.model_dump(mode="json")
    delivery = await manager.publish(context, lambda _: _StubPullRequestProvider())
    branch = delivery["repositories"][0]["work_branch"]
    assert "return left + right" in _git("show", f"{branch}:calculator.py", cwd=remote)
    assert delivery["repositories"][0]["pull_request_number"] == 23


async def test_deepseek_acceptance_tool_loop_selects_and_runs_independent_test(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # This standalone test container does not mount the production executor socket.
    monkeypatch.delenv("SANDBOX_EXECUTOR_SOCKET", raising=False)
    repository = tmp_path / "repo-1"
    repository.mkdir()
    (repository / "calculator.py").write_text("def add(left, right):\n    return left + right\n")
    (repository / "test_calculator.py").write_text(
        "from calculator import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n"
    )
    provider = OpenAICompatibleProvider(
        os.getenv("LLM_BASE_URL", "https://api.deepseek.com"),
        os.environ["DEEPSEEK_API_KEY"],
        os.getenv("LLM_MODEL", "deepseek-v4-flash"),
        timeout=float(os.getenv("LIVE_AI_TIMEOUT_SECONDS", "60")),
        max_tokens=int(os.getenv("LIVE_AI_MAX_TOKENS", "1500")),
    )
    context = {
        "requirement_id": "live-acceptance-1",
        "title": "验收加法函数",
        "description": "calculator.add 必须返回两数之和，并由验收工程师独立运行测试。",
        "artifacts": {
            "clarification_spec": {
                "acceptance_criteria": [
                    {
                        "id": "AC-1",
                        "description": "add(2, 3) 返回 5",
                        "verification_method": "在 repo-1 运行 pytest -q",
                        "priority": "must",
                    }
                ]
            },
            "architecture_plan": {"test_strategy": ["在 repo-1 运行 pytest -q"]},
            "verification_manifest": {
                "workspace_root": str(tmp_path),
                "checkout_type": "published_heads",
                "repositories": [{"repository_id": "repo-1", "relative_path": "repo-1"}],
            },
        },
    }

    report, response = await AcceptanceToolLoop(provider, max_steps=8).run(context)

    assert report.approved is True
    assert report.criteria[0].criterion_id == "AC-1"
    assert any(
        item.get("source") == "agent4_independent"
        and item.get("type") == "command"
        and item.get("status") == "passed"
        for item in report.regression_results
    )
    assert response.model


class _StubPullRequestProvider(GitProvider):
    async def create_or_update_pull_request(self, repository, head, base, title, body):
        return PullRequestRef(23, "https://example.invalid/pr/23", "unused", "open")

    async def get_repository(self, external_id): return {}
    async def create_branch(self, repository, branch, base_sha): return base_sha
    async def get_checks(self, repository, sha): return []
    async def merge(self, repository, number, expected_head_sha): return expected_head_sha
    def verify_webhook(self, body, headers): return True


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()
