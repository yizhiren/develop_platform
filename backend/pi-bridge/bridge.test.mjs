import assert from "node:assert/strict";
import test from "node:test";

import {
  createModels,
  fauxAssistantMessage,
  fauxProvider,
  fauxToolCall
} from "@earendil-works/pi-ai";

import { runAgentSession } from "./bridge.mjs";

const tools = [
  {
    name: "list_files",
    label: "List files",
    description: "List repository files",
    parameters: {
      type: "object",
      properties: { path: { type: "string" } },
      additionalProperties: false
    }
  },
  {
    name: "read_file",
    label: "Read file",
    description: "Read repository file",
    parameters: {
      type: "object",
      properties: { path: { type: "string" } },
      required: ["path"],
      additionalProperties: false
    }
  },
  {
    name: "finish_clarification",
    label: "Finish clarification",
    description: "Return the final report",
    parameters: {
      type: "object",
      properties: { report: { type: "object" } },
      required: ["report"],
      additionalProperties: false
    }
  }
];

test("Pi Agent Core executes registered tools sequentially and terminates on finish", async () => {
  const faux = fauxProvider();
  const models = createModels();
  models.setProvider(faux.provider);
  faux.setResponses([
    (_context, options) => {
      assert.equal(options.maxTokens, 0);
      return fauxAssistantMessage(fauxToolCall("list_files", { path: "." }));
    },
    fauxAssistantMessage(fauxToolCall("read_file", { path: "repo/app.py" })),
    fauxAssistantMessage(
      fauxToolCall("finish_clarification", { report: { summary: "done" } })
    )
  ]);
  const calls = [];
  const result = await runAgentSession(
    {
      system_prompt: "Inspect before finishing",
      user_prompt: "Fix CI",
      tools,
      terminal_tools: ["finish_clarification"],
      max_turns: 8
    },
    async (call) => {
      calls.push(call);
      return {
        ok: true,
        observation: { type: call.name },
        terminate: call.name === "finish_clarification"
      };
    },
    { models, model: faux.getModel() }
  );

  assert.deepEqual(calls.map((item) => item.name), [
    "list_files",
    "read_file",
    "finish_clarification"
  ]);
  assert.equal(result.terminal_tool_called, true);
  assert.equal(result.turns, 3);
});

test("Pi Agent Core follows up when the model answers without finishing", async () => {
  const faux = fauxProvider();
  const models = createModels();
  models.setProvider(faux.provider);
  faux.setResponses([
    fauxAssistantMessage("I am done"),
    fauxAssistantMessage(
      fauxToolCall("finish_clarification", { report: { summary: "done" } })
    )
  ]);
  const calls = [];
  const result = await runAgentSession(
    {
      system_prompt: "Use finish",
      user_prompt: "Clarify",
      tools,
      terminal_tools: ["finish_clarification"],
      max_turns: 4
    },
    async (call) => {
      calls.push(call);
      return { ok: true, observation: { type: call.name }, terminate: true };
    },
    { models, model: faux.getModel() }
  );

  assert.deepEqual(calls.map((item) => item.name), ["finish_clarification"]);
  assert.equal(result.terminal_tool_called, true);
  assert.equal(result.turns, 2);
});

test("Pi Agent Core allows a terminal result on turn fifty", async () => {
  const faux = fauxProvider();
  const models = createModels();
  models.setProvider(faux.provider);
  faux.setResponses([
    ...Array.from({ length: 49 }, () =>
      fauxAssistantMessage(fauxToolCall("list_files", { path: "." }))
    ),
    fauxAssistantMessage(
      fauxToolCall("finish_clarification", { report: { summary: "done" } })
    )
  ]);

  const result = await runAgentSession(
    {
      system_prompt: "Investigate autonomously",
      user_prompt: "Fix CI",
      tools,
      terminal_tools: ["finish_clarification"],
      max_turns: 50
    },
    async (call) => ({
      ok: true,
      observation: { type: call.name },
      terminate: call.name === "finish_clarification"
    }),
    { models, model: faux.getModel() }
  );

  assert.equal(result.turns, 50);
  assert.equal(result.terminal_tool_called, true);
  assert.equal(result.diagnostics.max_turns, 50);
  assert.equal(result.diagnostics.tool_calls, 50);
  assert.deepEqual(result.diagnostics.tool_call_counts, {
    list_files: 49,
    finish_clarification: 1
  });
});

test("Pi Agent Core reports an unrecoverable budget error after fifty turns", async () => {
  const faux = fauxProvider();
  const models = createModels();
  models.setProvider(faux.provider);
  faux.setResponses(
    Array.from({ length: 50 }, (_, index) => fauxAssistantMessage(`investigating ${index}`))
  );

  await assert.rejects(
    () =>
      runAgentSession(
        {
          system_prompt: "Investigate autonomously",
          user_prompt: "Fix CI",
          tools,
          terminal_tools: ["finish_clarification"],
          max_turns: 50
        },
        async () => ({ ok: true, observation: {}, terminate: true }),
        { models, model: faux.getModel() }
      ),
    (error) => {
      assert.equal(error.code, "agent.pi_turn_budget_exhausted");
      assert.equal(error.diagnostics.turns, 50);
      assert.equal(error.diagnostics.max_turns, 50);
      assert.equal(error.diagnostics.terminal_tool_called, false);
      assert.ok(error.usage.prompt_tokens > 0);
      return true;
    }
  );
});
