import { describe, expect, it } from "vitest";
import { EMPTY_USAGE, type OpenSessionResponse, type ServerEvent } from "../api/types";
import { initialRuntimeState, runtimeReducer } from "./runtime";

const opened: OpenSessionResponse = {
  session_id: "session-1",
  thread_id: "agent",
  status: "ready",
  agent_name: "default",
  workspace_root: "/workspace",
  provider: "minimax",
  model: "MiniMax-M2",
  model_mode: "",
  context_window: 1000,
  usage: { ...EMPTY_USAGE, input_tokens: 10, total_tokens: 10 },
  history: [],
  status_slots: {},
};

function event(type: string, data: Record<string, unknown>): ServerEvent {
  return {
    protocol_version: "xbotv2.v3",
    session_id: "session-1",
    thread_id: "agent",
    request_id: "request-1",
    sequence: 1,
    type,
    data,
  };
}

describe("runtimeReducer", () => {
  it("assembles streaming reasoning and assistant content once", () => {
    let state = runtimeReducer(initialRuntimeState, { type: "opened", session: opened });
    state = runtimeReducer(state, { type: "event", event: event("assistant_message_delta", { reasoning: "inspect " }) });
    state = runtimeReducer(state, { type: "event", event: event("assistant_message_delta", { content: "hello" }) });
    state = runtimeReducer(state, { type: "event", event: event("assistant_message", { content: "hello", tool_calls: [] }) });

    expect(state.entries).toHaveLength(1);
    expect(state.entries[0]).toMatchObject({
      kind: "message",
      role: "assistant",
      content: "hello",
      reasoning: "inspect ",
      streaming: false,
    });
  });

  it("accumulates usage deltas but replaces the current context count", () => {
    let state = runtimeReducer(initialRuntimeState, { type: "opened", session: opened });
    state = runtimeReducer(state, {
      type: "event",
      event: event("usage", {
        input_tokens: 40,
        output_tokens: 5,
        total_tokens: 45,
        requests: 1,
        context_tokens: 250,
      }),
    });

    expect(state.usage).toEqual({
      input_tokens: 50,
      output_tokens: 5,
      total_tokens: 55,
      requests: 1,
      context_tokens: 250,
    });
  });

  it("queues interactions in event order and resolves one at a time", () => {
    let state = runtimeReducer(initialRuntimeState, { type: "opened", session: opened });
    state = runtimeReducer(state, {
      type: "event",
      event: event("permission_request", {
        request_id: "permission-1",
        source: "tool",
        reason: "write file",
        tool_call: { id: "call-1", name: "filesystem_write", args: { path: "a.txt" } },
      }),
    });
    state = runtimeReducer(state, {
      type: "event",
      event: event("user_input_required", {
        request_id: "input-1",
        source: "ask_user",
        tool_call_id: "call-2",
        question: "Choose one",
        options: [
          { label: "A", description: "First" },
          { label: "B", description: "Second" },
        ],
      }),
    });

    expect(state.interactions.map((item) => item.request_id)).toEqual(["permission-1", "input-1"]);
    state = runtimeReducer(state, { type: "interaction_resolved", requestId: "permission-1" });
    expect(state.interactions[0].request_id).toBe("input-1");
  });

  it("updates tool results without duplicating the call", () => {
    let state = runtimeReducer(initialRuntimeState, { type: "opened", session: opened });
    state = runtimeReducer(state, {
      type: "event",
      event: event("tool_calls_started", {
        tool_calls: [{ id: "call-1", name: "shell", args: { command: "pwd" } }],
      }),
    });
    state = runtimeReducer(state, {
      type: "event",
      event: event("tool_result", {
        tool_call_id: "call-1",
        name: "shell",
        content: "/workspace",
        status: "success",
      }),
    });

    expect(state.entries).toHaveLength(1);
    expect(state.entries[0]).toMatchObject({ kind: "tool", status: "success", result: "/workspace" });
  });

  it("renders a persisted tool result even when its call is outside display history", () => {
    const state = runtimeReducer(initialRuntimeState, {
      type: "history",
      history: [{
        role: "tool",
        content: "cached output",
        tool_calls: [],
        tool_call_id: "call-orphan",
        status: "success",
        data: null,
        error: null,
        artifacts: [],
      }],
    });

    expect(state.entries[0]).toMatchObject({
      kind: "tool",
      toolCallId: "call-orphan",
      result: "cached output",
    });
  });
});
