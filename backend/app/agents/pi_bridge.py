from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .providers import ModelResponse, OpenAICompatibleProvider
from .runtime import AgentOutputError


MAX_PROTOCOL_LINE_BYTES = 2 * 1024 * 1024
MAX_TOOL_CALLS = 100


class PiBridgeError(AgentOutputError):
    code = "agent.pi_bridge_failed"
    retryable = True


class PiBridgeUnavailable(PiBridgeError):
    code = "agent.pi_bridge_unavailable"
    retryable = False


class PiBridgeTimeout(PiBridgeError):
    code = "agent.pi_bridge_timeout"
    retryable = True


class PiBridgeTurnBudgetExhausted(PiBridgeError):
    code = "agent.pi_turn_budget_exhausted"
    retryable = False


@dataclass(frozen=True)
class PiToolDefinition:
    name: str
    label: str
    description: str
    parameters: dict[str, Any]

    def as_payload(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "label": self.label,
            "description": self.description,
            "parameters": self.parameters,
        }


@dataclass(frozen=True)
class PiToolResult:
    observation: dict[str, Any]
    terminate: bool = False
    details: dict[str, Any] | None = None


PiToolHandler = Callable[[str, dict[str, Any]], Awaitable[PiToolResult]]


class PiAgentCoreBridge:
    """Bidirectional JSON-lines bridge between Python tools and Pi Agent Core."""

    def __init__(
        self,
        bridge_path: Path,
        timeout_seconds: int = 900,
        node_executable: str = "node",
    ) -> None:
        self.bridge_path = bridge_path
        self.timeout_seconds = min(max(timeout_seconds, 30), 900)
        self.node_executable = node_executable

    async def run(
        self,
        *,
        provider: OpenAICompatibleProvider,
        system_prompt: str,
        user_prompt: str,
        tools: list[PiToolDefinition],
        terminal_tools: set[str],
        handler: PiToolHandler,
        max_turns: int = 32,
    ) -> ModelResponse:
        if not self.bridge_path.is_file():
            raise PiBridgeUnavailable("Pi Agent Core bridge is not installed")
        if not tools:
            raise PiBridgeUnavailable("Pi Agent Core requires at least one registered tool")
        if not provider.api_key:
            raise PiBridgeError("model API key is not configured")

        registered = {tool.name for tool in tools}
        if len(registered) != len(tools) or not terminal_tools.issubset(registered):
            raise PiBridgeUnavailable("Pi tool registration is invalid")

        try:
            process = await asyncio.create_subprocess_exec(
                self.node_executable,
                str(self.bridge_path),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=MAX_PROTOCOL_LINE_BYTES + 1,
            )
        except (FileNotFoundError, OSError) as exc:
            raise PiBridgeUnavailable("Node.js runtime for Pi Agent Core is unavailable") from exc
        assert process.stdin is not None
        assert process.stdout is not None
        assert process.stderr is not None

        stderr_task = asyncio.create_task(_drain_stderr(process.stderr))
        start = {
            "type": "start",
            "payload": {
                "provider": {
                    "base_url": provider.base_url,
                    "api_key": provider.api_key,
                    "model": provider.model,
                    "max_tokens": provider.max_tokens,
                    "max_tokens_field": provider.max_tokens_field,
                },
                "system_prompt": system_prompt,
                "user_prompt": user_prompt,
                "tools": [tool.as_payload() for tool in tools],
                "terminal_tools": sorted(terminal_tools),
                "max_turns": min(max(max_turns, 1), 50),
            },
        }
        tool_calls = 0
        final_message: dict[str, Any] | None = None
        try:
            async with asyncio.timeout(self.timeout_seconds):
                await _write_message(process.stdin, start)
                while True:
                    message = await _read_message(process.stdout)
                    message_type = message.get("type")
                    if message_type == "tool_call":
                        tool_calls += 1
                        if tool_calls > MAX_TOOL_CALLS:
                            raise PiBridgeError("Pi Agent Core exceeded the tool-call budget")
                        request_id = message.get("request_id")
                        name = str(message.get("name") or "")
                        arguments = message.get("args")
                        response: dict[str, Any] = {
                            "type": "tool_result",
                            "request_id": request_id,
                        }
                        if name not in registered or not isinstance(arguments, dict):
                            response.update(ok=False, error="tool request is not registered or invalid")
                        else:
                            try:
                                result = await handler(name, arguments)
                                response.update(
                                    ok=True,
                                    observation=result.observation,
                                    details=result.details,
                                    terminate=result.terminate,
                                )
                            except Exception as exc:  # Tool errors are observations the model can recover from.
                                response.update(ok=False, error=_safe_error(exc))
                        await _write_message(process.stdin, response)
                        continue
                    if message_type == "result":
                        final_message = message
                        break
                    raise PiBridgeError("Pi Agent Core returned an unexpected protocol message")

                process.stdin.close()
                with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                    await process.stdin.wait_closed()
                return_code = await process.wait()
        except TimeoutError as exc:
            await _stop_process(process)
            raise PiBridgeTimeout(
                f"Pi Agent Core exceeded the {self.timeout_seconds}-second timeout"
            ) from exc
        except (asyncio.IncompleteReadError, json.JSONDecodeError, ValueError) as exc:
            await _stop_process(process)
            raise PiBridgeError("Pi Agent Core protocol failed") from exc
        except Exception:
            await _stop_process(process)
            raise
        finally:
            stderr = await stderr_task

        if final_message is None:
            raise PiBridgeError("Pi Agent Core stopped without a result")
        prompt_tokens = _bounded_int(final_message.get("prompt_tokens"))
        completion_tokens = _bounded_int(final_message.get("completion_tokens"))
        token_usage = prompt_tokens + completion_tokens
        diagnostics = _safe_diagnostics(final_message.get("diagnostics"))
        if final_message.get("status") != "completed" or return_code != 0:
            detail = _safe_text(final_message.get("error_message")) or _safe_text(stderr)
            if provider.api_key:
                detail = detail.replace(provider.api_key, "[redacted]")
            error_type = (
                PiBridgeTurnBudgetExhausted
                if final_message.get("error_code") == PiBridgeTurnBudgetExhausted.code
                else PiBridgeError
            )
            raise error_type(
                detail or "Pi Agent Core failed",
                token_usage=token_usage,
                diagnostics=diagnostics,
            )
        if not final_message.get("terminal_tool_called"):
            raise PiBridgeTurnBudgetExhausted(
                "Pi Agent Core stopped without submitting a structured result",
                token_usage=token_usage,
                diagnostics=diagnostics,
            )
        return ModelResponse(
            content="",
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            model=_safe_text(final_message.get("model"), 120) or provider.model,
            diagnostics=diagnostics,
        )


async def _write_message(writer: asyncio.StreamWriter, message: dict[str, Any]) -> None:
    encoded = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode() + b"\n"
    if len(encoded) > MAX_PROTOCOL_LINE_BYTES:
        raise PiBridgeError("Pi Agent Core protocol message exceeds the size limit")
    writer.write(encoded)
    await writer.drain()


async def _read_message(reader: asyncio.StreamReader) -> dict[str, Any]:
    raw = await reader.readline()
    if not raw:
        raise PiBridgeError("Pi Agent Core closed the protocol unexpectedly")
    if len(raw) > MAX_PROTOCOL_LINE_BYTES or not raw.endswith(b"\n"):
        raise PiBridgeError("Pi Agent Core protocol line exceeds the size limit")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise PiBridgeError("Pi Agent Core protocol message must be an object")
    return value


async def _drain_stderr(reader: asyncio.StreamReader) -> str:
    captured = bytearray()
    while chunk := await reader.read(8_192):
        remaining = 32_000 - len(captured)
        if remaining > 0:
            captured.extend(chunk[:remaining])
    return captured.decode(errors="replace")


async def _stop_process(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    process.terminate()
    try:
        await asyncio.wait_for(process.wait(), 2)
    except TimeoutError:
        process.kill()
        await process.wait()


def _safe_error(exc: Exception) -> str:
    return _safe_text(str(exc)) or "tool execution failed"


def _safe_text(value: Any, limit: int = 2_000) -> str:
    return " ".join(str(value or "").split())[:limit]


def _bounded_int(value: Any) -> int:
    try:
        return min(max(int(value or 0), 0), 2**31 - 1)
    except (TypeError, ValueError):
        return 0


def _safe_diagnostics(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, Any] = {}
    for key in ("turns", "max_turns", "tool_calls", "tool_errors"):
        result[key] = _bounded_int(value.get(key))
    result["last_stop_reason"] = _safe_text(value.get("last_stop_reason"), 80)
    result["terminal_tool_called"] = bool(value.get("terminal_tool_called"))
    raw_counts = value.get("tool_call_counts")
    if isinstance(raw_counts, dict):
        result["tool_call_counts"] = {
            _safe_text(key, 64): _bounded_int(count)
            for key, count in list(raw_counts.items())[:100]
            if _safe_text(key, 64)
        }
    return result


__all__ = [
    "PiAgentCoreBridge",
    "PiBridgeError",
    "PiBridgeTimeout",
    "PiBridgeTurnBudgetExhausted",
    "PiBridgeUnavailable",
    "PiToolDefinition",
    "PiToolResult",
]
