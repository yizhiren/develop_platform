import json
import sys
from pathlib import Path

import pytest

from app.agents.pi_bridge import (
    PiAgentCoreBridge,
    PiBridgeError,
    PiBridgeTurnBudgetExhausted,
    PiToolDefinition,
    PiToolResult,
)
from app.agents.providers import OpenAICompatibleProvider


@pytest.mark.asyncio
async def test_python_bridge_serves_tools_over_bidirectional_protocol(tmp_path: Path) -> None:
    bridge_script = tmp_path / "fake_bridge.py"
    bridge_script.write_text(
        """import json, sys
start = json.loads(sys.stdin.readline())
assert start['type'] == 'start'
assert start['payload']['provider']['api_key']
print(json.dumps({'type': 'tool_call', 'request_id': 1, 'tool_call_id': 'call-1', 'name': 'finish_test', 'args': {'value': 7}}), flush=True)
tool_result = json.loads(sys.stdin.readline())
assert tool_result['ok'] is True
print(json.dumps({'type': 'result', 'status': 'completed', 'prompt_tokens': 3, 'completion_tokens': 2, 'model': 'stub', 'terminal_tool_called': True}), flush=True)
"""
    )
    bridge = PiAgentCoreBridge(
        bridge_script,
        timeout_seconds=30,
        node_executable=sys.executable,
    )
    provider = OpenAICompatibleProvider("https://example.test", "secret", "stub")
    calls: list[tuple[str, dict]] = []

    async def handler(name: str, arguments: dict) -> PiToolResult:
        calls.append((name, arguments))
        return PiToolResult(observation={"accepted": True}, terminate=True)

    response = await bridge.run(
        provider=provider,
        system_prompt="test",
        user_prompt=json.dumps({"request": "test"}),
        tools=[
            PiToolDefinition(
                name="finish_test",
                label="Finish",
                description="Finish test",
                parameters={
                    "type": "object",
                    "properties": {"value": {"type": "integer"}},
                    "required": ["value"],
                },
            )
        ],
        terminal_tools={"finish_test"},
        handler=handler,
    )

    assert calls == [("finish_test", {"value": 7})]
    assert response.prompt_tokens == 3
    assert response.completion_tokens == 2
    assert response.model == "stub"


@pytest.mark.asyncio
async def test_python_bridge_redacts_provider_key_from_failures(tmp_path: Path) -> None:
    bridge_script = tmp_path / "failed_bridge.py"
    bridge_script.write_text(
        """import json, sys
start = json.loads(sys.stdin.readline())
secret = start['payload']['provider']['api_key']
print(json.dumps({'type': 'result', 'status': 'failed', 'error_message': f'upstream rejected {secret}'}), flush=True)
raise SystemExit(1)
"""
    )
    bridge = PiAgentCoreBridge(
        bridge_script,
        timeout_seconds=30,
        node_executable=sys.executable,
    )
    provider = OpenAICompatibleProvider(
        "https://example.test",
        "super-secret-provider-key",
        "stub",
    )

    async def handler(name: str, arguments: dict) -> PiToolResult:
        raise AssertionError("failed bridge must not call tools")

    with pytest.raises(PiBridgeError) as captured:
        await bridge.run(
            provider=provider,
            system_prompt="test",
            user_prompt="test",
            tools=[
                PiToolDefinition(
                    name="finish_test",
                    label="Finish",
                    description="Finish test",
                    parameters={"type": "object", "properties": {}},
                )
            ],
            terminal_tools={"finish_test"},
            handler=handler,
        )

    assert "super-secret-provider-key" not in str(captured.value)
    assert "[redacted]" in str(captured.value)


@pytest.mark.asyncio
async def test_python_bridge_preserves_turn_budget_diagnostics_and_usage(tmp_path: Path) -> None:
    bridge_script = tmp_path / "budget_bridge.py"
    bridge_script.write_text(
        """import json, sys
json.loads(sys.stdin.readline())
print(json.dumps({'type': 'result', 'status': 'failed', 'error_code': 'agent.pi_turn_budget_exhausted', 'error_message': 'exhausted 50 turns', 'prompt_tokens': 120, 'completion_tokens': 8, 'diagnostics': {'turns': 50, 'max_turns': 50, 'tool_calls': 42, 'tool_errors': 2, 'tool_call_counts': {'read_file': 42}, 'last_stop_reason': 'stop', 'terminal_tool_called': False}}), flush=True)
raise SystemExit(1)
"""
    )
    bridge = PiAgentCoreBridge(
        bridge_script,
        timeout_seconds=30,
        node_executable=sys.executable,
    )
    provider = OpenAICompatibleProvider("https://example.test", "secret", "stub")

    async def handler(name: str, arguments: dict) -> PiToolResult:
        raise AssertionError("budget bridge must not call tools")

    with pytest.raises(PiBridgeTurnBudgetExhausted) as captured:
        await bridge.run(
            provider=provider,
            system_prompt="test",
            user_prompt="test",
            tools=[
                PiToolDefinition(
                    name="finish_test",
                    label="Finish",
                    description="Finish test",
                    parameters={"type": "object", "properties": {}},
                )
            ],
            terminal_tools={"finish_test"},
            handler=handler,
            max_turns=50,
        )

    assert captured.value.retryable is False
    assert captured.value.token_usage == 128
    assert captured.value.diagnostics["turns"] == 50
    assert captured.value.diagnostics["tool_call_counts"] == {"read_file": 42}
