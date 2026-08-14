import process from "node:process";
import readline from "node:readline";
import { pathToFileURL } from "node:url";

import { Agent } from "@earendil-works/pi-agent-core";
import { createModels, createProvider } from "@earendil-works/pi-ai";
import { openAICompletionsApi } from "@earendil-works/pi-ai/api/openai-completions.lazy";

const MAX_PROTOCOL_LINE_BYTES = 2 * 1024 * 1024;
const DEFAULT_MAX_TURNS = 32;

class PiSessionError extends Error {
  constructor(code, message, usage, diagnostics) {
    super(message);
    this.code = code;
    this.usage = usage;
    this.diagnostics = diagnostics;
  }
}

function assertObject(value, label) {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new Error(`${label} must be an object`);
  }
  return value;
}

function safeErrorMessage(error, secret = "") {
  let message = error instanceof Error ? error.message : String(error);
  if (secret) {
    message = message.split(secret).join("[redacted]");
  }
  return message.replace(/[\r\n\t]+/g, " ").slice(0, 2000) || "Pi bridge failed";
}

function normalizeBaseUrl(value) {
  const result = String(value || "").replace(/\/+$/, "");
  if (!result.startsWith("https://") && !result.startsWith("http://")) {
    throw new Error("provider base_url must be HTTP(S)");
  }
  return result;
}

function buildProvider(start) {
  const input = assertObject(start.provider, "provider");
  const apiKey = String(input.api_key || "");
  if (!apiKey) {
    throw new Error("model API key is not configured");
  }
  const providerId = "forgeflow-openai-compatible";
  const modelId = String(input.model || "").trim();
  if (!modelId) {
    throw new Error("provider model is missing");
  }
  const baseUrl = normalizeBaseUrl(input.base_url);
  const maxTokensField = input.max_tokens_field === "max_completion_tokens"
    ? "max_completion_tokens"
    : "max_tokens";
  const model = {
    id: modelId,
    name: modelId,
    api: "openai-completions",
    provider: providerId,
    baseUrl,
    reasoning: false,
    input: ["text"],
    cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
    contextWindow: Number(input.context_window || 128000),
    maxTokens: Number(input.max_tokens || 4096),
    compat: {
      supportsStore: false,
      supportsDeveloperRole: false,
      supportsReasoningEffort: false,
      supportsStrictMode: false,
      supportsUsageInStreaming: true,
      maxTokensField,
      thinkingFormat: "deepseek"
    }
  };
  const provider = createProvider({
    id: providerId,
    name: "ForgeFlow OpenAI-compatible provider",
    baseUrl,
    auth: {
      apiKey: {
        name: "ForgeFlow model credential",
        resolve: async () => ({ auth: {} })
      }
    },
    models: [model],
    api: openAICompletionsApi()
  });
  const models = createModels();
  models.setProvider(provider);
  return { models, model, apiKey };
}

function buildTools(definitions, exchange) {
  if (!Array.isArray(definitions) || definitions.length === 0) {
    throw new Error("at least one tool definition is required");
  }
  const names = new Set();
  return definitions.map((definition) => {
    const tool = assertObject(definition, "tool definition");
    const name = String(tool.name || "");
    if (!/^[a-z][a-z0-9_]{0,63}$/.test(name) || names.has(name)) {
      throw new Error("tool names must be unique snake_case identifiers");
    }
    names.add(name);
    const parameters = assertObject(tool.parameters, `${name} parameters`);
    return {
      name,
      label: String(tool.label || name),
      description: String(tool.description || ""),
      parameters,
      executionMode: "sequential",
      execute: async (toolCallId, args) => {
        const response = assertObject(
          await exchange({ tool_call_id: toolCallId, name, args }),
          "tool response"
        );
        if (!response.ok) {
          throw new Error(String(response.error || "tool execution failed").slice(0, 2000));
        }
        const observation = response.observation ?? {};
        return {
          content: [{ type: "text", text: JSON.stringify(observation) }],
          details: response.details ?? observation,
          terminate: Boolean(response.terminate)
        };
      }
    };
  });
}

function usageFromMessages(messages) {
  let promptTokens = 0;
  let completionTokens = 0;
  for (const message of messages) {
    if (message?.role !== "assistant" || !message.usage) continue;
    promptTokens += Number(message.usage.input || 0);
    promptTokens += Number(message.usage.cacheRead || 0);
    promptTokens += Number(message.usage.cacheWrite || 0);
    completionTokens += Number(message.usage.output || 0);
  }
  return { prompt_tokens: promptTokens, completion_tokens: completionTokens };
}

export async function runAgentSession(startInput, exchange, overrides = {}) {
  const start = assertObject(startInput, "start payload");
  const configured = overrides.models && overrides.model
    ? { models: overrides.models, model: overrides.model, apiKey: "" }
    : buildProvider(start);
  const tools = buildTools(start.tools, exchange);
  const maxTurns = Math.min(Math.max(Number(start.max_turns || DEFAULT_MAX_TURNS), 1), 50);
  let turnCount = 0;
  let terminalToolCalled = false;
  let toolCallCount = 0;
  let toolErrorCount = 0;
  let lastStopReason = "";
  const toolCallCounts = {};
  const terminalTools = new Set(
    Array.isArray(start.terminal_tools) ? start.terminal_tools.map(String) : []
  );
  const terminalToolNames = [...terminalTools].sort();
  const turnBudgetInstruction =
    `你最多有 ${maxTurns} 个模型轮次。请自行规划代码调查、证据核对和结论整理；` +
    `必须在第 ${maxTurns} 轮结束前调用 ${terminalToolNames.join(" 或 ")} 提交结构化结果。`;

  const streamFn = (model, context, options = {}) => {
    // pi-ai omits the OpenAI-compatible max_tokens field when maxTokens is 0.
    // Leave response sizing to the upstream model instead of imposing a
    // platform-side per-turn output ceiling.
    const requestOptions = { ...options, maxTokens: 0 };
    return configured.models.streamSimple(model, context, requestOptions);
  };

  const agent = new Agent({
    initialState: {
      systemPrompt: `${String(start.system_prompt || "")}\n${turnBudgetInstruction}`,
      model: configured.model,
      thinkingLevel: "off",
      tools
    },
    streamFn,
    getApiKey: () => configured.apiKey || undefined,
    toolExecution: "sequential",
    beforeToolCall: async ({ toolCall }) => {
      if (!tools.some((tool) => tool.name === toolCall.name)) {
        return { block: true, reason: "tool is not registered" };
      }
      return undefined;
    },
    afterToolCall: async ({ toolCall, isError, result }) => {
      if (!isError && terminalTools.has(toolCall.name) && result.terminate) {
        terminalToolCalled = true;
      }
      return undefined;
    },
    shouldStopAfterTurn: () => terminalToolCalled || turnCount >= maxTurns
  });

  agent.subscribe((event) => {
    if (event.type !== "turn_end") return;
    turnCount += 1;
    lastStopReason = String(event.message.stopReason || "");
    const calledTools = event.message.content.filter((item) => item.type === "toolCall");
    toolCallCount += calledTools.length;
    for (const call of calledTools) {
      const name = String(call.name || "unknown");
      toolCallCounts[name] = Number(toolCallCounts[name] || 0) + 1;
    }
    toolErrorCount += event.toolResults.filter((item) => item.isError).length;
    const calledTool = calledTools.length > 0;
    if (!terminalToolCalled && !calledTool && turnCount < maxTurns) {
      agent.followUp({
        role: "user",
        content: "不要输出普通文本作为最终结果。继续检查必要证据，并调用唯一合适的 finish_* 工具提交结构化结果。",
        timestamp: Date.now()
      });
    }
  });

  await agent.prompt(String(start.user_prompt || ""));
  const state = agent.state;
  const usage = usageFromMessages(state.messages);
  const diagnostics = {
    turns: turnCount,
    max_turns: maxTurns,
    tool_calls: toolCallCount,
    tool_errors: toolErrorCount,
    tool_call_counts: toolCallCounts,
    last_stop_reason: lastStopReason,
    terminal_tool_called: terminalToolCalled
  };
  if (state.errorMessage) {
    throw new PiSessionError(
      "agent.pi_bridge_failed",
      state.errorMessage,
      usage,
      diagnostics
    );
  }
  if (!terminalToolCalled) {
    throw new PiSessionError(
      "agent.pi_turn_budget_exhausted",
      `Pi Agent Core exhausted ${maxTurns} turns without calling ${terminalToolNames.join(" or ")}`,
      usage,
      diagnostics
    );
  }
  return {
    ...usage,
    model: configured.model.id,
    turns: turnCount,
    terminal_tool_called: terminalToolCalled,
    diagnostics
  };
}

async function readProtocolLine(iterator) {
  const next = await iterator.next();
  if (next.done) throw new Error("protocol input closed unexpectedly");
  if (Buffer.byteLength(next.value, "utf8") > MAX_PROTOCOL_LINE_BYTES) {
    throw new Error("protocol line exceeds size limit");
  }
  return assertObject(JSON.parse(next.value), "protocol message");
}

async function main() {
  const lines = readline.createInterface({ input: process.stdin, crlfDelay: Infinity });
  const iterator = lines[Symbol.asyncIterator]();
  let secret = "";
  const write = (message) => process.stdout.write(`${JSON.stringify(message)}\n`);
  try {
    const startMessage = await readProtocolLine(iterator);
    if (startMessage.type !== "start") throw new Error("first protocol message must be start");
    secret = String(startMessage.payload?.provider?.api_key || "");
    let requestId = 0;
    const result = await runAgentSession(startMessage.payload, async (call) => {
      requestId += 1;
      write({ type: "tool_call", request_id: requestId, ...call });
      const response = await readProtocolLine(iterator);
      if (response.type !== "tool_result" || response.request_id !== requestId) {
        throw new Error("tool response does not match request");
      }
      return response;
    });
    write({ type: "result", status: "completed", ...result });
  } catch (error) {
    const usage = error instanceof PiSessionError ? error.usage : {};
    const diagnostics = error instanceof PiSessionError ? error.diagnostics : undefined;
    write({
      type: "result",
      status: "failed",
      error_code: error instanceof PiSessionError ? error.code : "agent.pi_bridge_failed",
      error_message: safeErrorMessage(error, secret),
      prompt_tokens: Number(usage?.prompt_tokens || 0),
      completion_tokens: Number(usage?.completion_tokens || 0),
      ...(diagnostics ? { diagnostics } : {})
    });
    process.exitCode = 1;
  } finally {
    lines.close();
  }
}

if (import.meta.url === pathToFileURL(process.argv[1] || "").href) {
  await main();
}
