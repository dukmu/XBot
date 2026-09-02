import { describe, expect, it } from "vitest";
import { EMPTY_SESSION_STATS, EMPTY_USAGE, type OpenSessionResponse, type ServerEvent, type ThreadSummary } from "../api/types";
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
  session_stats: { ...EMPTY_SESSION_STATS },
  history: [],
  event_cursor: 0,
  status_slots: {},
  pending_inputs: [],
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
  it("replaces session timing from authoritative terminal events", () => {
    const state = runtimeReducer(
      runtimeReducer(initialRuntimeState, { type: "opened", session: opened }),
      { type: "event", event: event("turn_finished", {
        turn: 1,
        session_stats: {
          turns: 1, steps: 2, llm_ms: 1200, tool_ms: 300,
          ttft_ms: 200, ttft_steps: 1, decode_ms: 1000, decode_tokens: 25,
        },
      }) },
    );

    expect(state.sessionStats).toMatchObject({
      turns: 1,
      steps: 2,
      llm_ms: 1200,
      tool_ms: 300,
      decode_tokens: 25,
    });
  });

  it("renders injected model context with provenance and content, not as a user", () => {
    const state = runtimeReducer(initialRuntimeState, {
      type: "history",
      history: [{
        role: "user",
        content: "job finished with result 42",
        tool_calls: [],
        tool_call_id: "",
        status: "",
        data: null,
        error: null,
        artifacts: [],
        images: [],
        runtime: { source: "task-1", event: "notification" },
      }],
      nextCursor: null,
    });

    expect(state.entries).toHaveLength(1);
    expect(state.entries[0]).toMatchObject({
      kind: "runtime",
      source: "task-1",
      event: "notification",
      content: "job finished with result 42",
    });
  });

  it("restores persisted assistant reasoning with its visible response", () => {
    const state = runtimeReducer(initialRuntimeState, {
      type: "history",
      history: [{
        role: "assistant",
        content: "final answer",
        reasoning: "inspect the state",
        tool_calls: [],
        tool_call_id: "",
        status: "",
        data: null,
        error: null,
        artifacts: [],
        images: [],
      }],
      nextCursor: null,
    });

    expect(state.entries).toHaveLength(1);
    expect(state.entries[0]).toMatchObject({
      kind: "message",
      role: "assistant",
      content: "final answer",
      reasoning: "inspect the state",
      streaming: false,
    });
  });

  it("uses authoritative history and message events without duplicating optimistic input", () => {
    let state = runtimeReducer(initialRuntimeState, { type: "opened", session: opened });
    state = runtimeReducer(state, {
      type: "user_message",
      id: "request-1",
      content: "hello",
      images: [],
    });
    expect(state.turnRunning).toBe(true);
    state = runtimeReducer(state, {
      type: "event",
      event: event("message", { id: "request-1", role: "user", content: "hello" }),
    });
    expect(state.entries.filter((entry) => entry.kind === "message")).toHaveLength(1);

    state = runtimeReducer(state, {
      type: "event",
      event: event("history_updated", {
        operation: "regenerate",
        turns: 1,
        history: [],
      }),
    });
    expect(state.entries).toEqual([]);
  });

  it("prepends one server page and advances its cursor", () => {
    let state = runtimeReducer(initialRuntimeState, { type: "opened", session: {
      ...opened,
      history_cursor: "20",
      history: [{
        role: "assistant", content: "new", tool_calls: [], tool_call_id: "", status: "",
        data: null, error: null, artifacts: [], images: [],
      }],
    } });
    state = runtimeReducer(state, {
      type: "history_prepend",
      history: [{
        role: "user", content: "old", tool_calls: [], tool_call_id: "", status: "",
        data: null, error: null, artifacts: [], images: [],
      }],
      nextCursor: null,
    });
    expect(state.entries.map((entry) => entry.kind === "message" ? entry.content : "")).toEqual(["old", "new"]);
    expect(state.historyCursor).toBeNull();
    expect(state.current).not.toHaveProperty("history");
  });

  it("keeps live entries when an older persisted page is prepended", () => {
    let state = runtimeReducer(initialRuntimeState, { type: "opened", session: {
      ...opened, history_cursor: "10",
    } });
    state = runtimeReducer(state, {
      type: "event",
      event: event("assistant_message", { content: "live answer", tool_calls: [] }),
    });
    state = runtimeReducer(state, {
      type: "history_prepend",
      history: [{
        role: "user", content: "old question", tool_calls: [], tool_call_id: "", status: "",
        data: null, error: null, artifacts: [], images: [],
      }],
      nextCursor: null,
    });
    expect(state.entries.map((entry) => entry.kind === "message" ? entry.content : "")).toEqual([
      "old question", "live answer",
    ]);
  });

  it("projects Todo state from its authoritative client event", () => {
    const state = runtimeReducer(initialRuntimeState, {
      type: "event",
      event: event("todo_updated", {
        kind: "todo_snapshot",
        schema_version: 1,
        items: [
          { content: "implement", status: "in_progress" },
          { content: "verify", status: "pending" },
        ],
      }),
    });
    expect(state.todos).toEqual([
      { content: "implement", status: "in_progress" },
      { content: "verify", status: "pending" },
    ]);
  });

  it("keeps pending input authoritative across queue events and turn failure", () => {
    let state = runtimeReducer(initialRuntimeState, {
      type: "event",
      event: event("queue_updated", { items: [{
        message_id: "queued-1",
        content: "continue later",
        target: "next-turn",
        source: "user",
        image_count: 0,
        artifact_count: 0,
      }] }),
    });
    expect(state.pendingInputs).toHaveLength(1);

    state = runtimeReducer(state, {
      type: "event",
      event: event("turn_failed", { message: "provider unavailable" }),
    });
    expect(state.pendingInputs.map((item) => item.message_id)).toEqual(["queued-1"]);
  });

  it("detaches all thread projections when the current session is deleted", () => {
    const state = runtimeReducer(
      {
        ...runtimeReducer(initialRuntimeState, { type: "opened", session: opened }),
        loading: true,
      },
      { type: "session_deleted", sessionId: opened.session_id },
    );

    expect(state).toMatchObject({
      loading: false,
      sessionAttached: false,
      eventStreamConnected: false,
      current: null,
      entries: [],
      threads: [],
    });
  });

  it("synchronizes thread metadata without replacing the attached workspace", () => {
    const thread: ThreadSummary = {
      session_id: "session-1",
      thread_id: "agent",
      status: "active",
      kind: "main",
      turn_status: "idle",
      parent_thread_id: "",
      agent: "reviewer",
      provider: "openai",
      model: "gpt",
      model_mode: "high",
      context_window: 2000,
      message_count: 4,
      usage: { ...EMPTY_USAGE, total_tokens: 25, context_tokens: 20 },
      session_stats: { ...EMPTY_SESSION_STATS },
      pending_interactions: [],
      status_slots: { goal: "active" },
      workspace_root: "/updated",
    };
    const state = runtimeReducer(
      runtimeReducer(initialRuntimeState, { type: "opened", session: opened }),
      { type: "thread_synced", thread },
    );

    expect(state.current).toMatchObject({
      agent_name: "reviewer", provider: "openai", model: "gpt", workspace_root: "/workspace",
    });
    expect(state.usage).toMatchObject({ total_tokens: 25, context_tokens: 20 });
  });

  it("adopts the running state of a session selected mid-turn", () => {
    const current = runtimeReducer(initialRuntimeState, { type: "opened", session: opened });
    const thread = {
      session_id: "session-1", thread_id: "agent", status: "active" as const,
      kind: "main" as const, turn_status: "running" as const, parent_thread_id: "",
      agent: "default", provider: "minimax", model: "MiniMax-M2", model_mode: "",
      context_window: 1000, message_count: 2, usage: opened.usage,
      session_stats: opened.session_stats, pending_interactions: [], status_slots: {},
    };

    expect(runtimeReducer(current, { type: "thread_synced", thread }).turnRunning).toBe(true);
  });

  it("assembles streaming reasoning and assistant content once", () => {
    let state = runtimeReducer(initialRuntimeState, { type: "opened", session: opened });
    const committedEntries = state.entries;
    state = runtimeReducer(state, { type: "event", event: event("assistant_message_delta", { reasoning: "inspect " }) });
    expect(state.entries).toBe(committedEntries);
    state = runtimeReducer(state, { type: "event", event: event("assistant_message_delta", { content: "hello" }) });
    expect(state.entries).toBe(committedEntries);
    expect(state.entries).toEqual([]);
    expect(state.assistantDraft).toMatchObject({ content: "hello", reasoning: "inspect " });
    state = runtimeReducer(state, { type: "event", event: event("assistant_message", { content: "hello", tool_calls: [] }) });

    expect(state.entries).toHaveLength(1);
    expect(state.entries[0]).toMatchObject({
      kind: "message",
      role: "assistant",
      content: "hello",
      reasoning: "inspect ",
      streaming: false,
    });
    expect(state.assistantDraft).toBeNull();
  });

  it("applies a streaming batch in wire order", () => {
    const state = runtimeReducer(initialRuntimeState, {
      type: "events",
      events: [
        event("assistant_message_delta", { content: "hello " }),
        event("assistant_message_delta", { content: "world" }),
        event("assistant_message", { content: "hello world", tool_calls: [] }),
      ],
    });

    expect(state.entries).toHaveLength(1);
    expect(state.entries[0]).toMatchObject({
      kind: "message",
      content: "hello world",
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
        cache_read_input_tokens: 30,
        cache_creation_input_tokens: 10,
        prompt_cache_write_tokens: 4,
      }),
    });

    expect(state.usage).toEqual({
      input_tokens: 50,
      output_tokens: 5,
      total_tokens: 55,
      requests: 1,
      context_tokens: 250,
      cache_read_input_tokens: 30,
      cache_creation_input_tokens: 10,
      prompt_cache_write_tokens: 4,
    });

    state = runtimeReducer(state, {
      type: "event",
      event: event("usage", {
        input_tokens: 0,
        output_tokens: 1,
        total_tokens: 1,
        requests: 1,
        context_tokens: 0,
      }),
    });
    expect(state.usage.context_tokens).toBe(0);
    expect(state.usage.total_tokens).toBe(56);
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

  it("closes a turn and preserves a visible error when the stream fails", () => {
    let state = runtimeReducer(initialRuntimeState, { type: "opened", session: opened });
    state = runtimeReducer(state, { type: "event", event: event("turn_started", { turn: 1 }) });
    state = runtimeReducer(state, { type: "event", event: event("assistant_message_delta", { content: "partial" }) });
    state = runtimeReducer(state, { type: "event", event: event("error", { message: "provider failed" }) });

    expect(state.turnRunning).toBe(false);
    expect(state.entries.at(-1)).toMatchObject({ kind: "notice", level: "error", content: "provider failed" });
    expect(state.entries.find((entry) => entry.kind === "message")).toMatchObject({ streaming: false });
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
        images: [],
      }],
    });

    expect(state.entries[0]).toMatchObject({
      kind: "tool",
      toolCallId: "call-orphan",
      result: "cached output",
    });
  });
});
