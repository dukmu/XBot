import { describe, expect, it } from "vitest";
import { EMPTY_SESSION_STATS, EMPTY_USAGE, type OpenSessionResponse, type ServerEvent, type ThreadSummary } from "../api/types";
import { applyViewEvent, historyEntries, initialRuntimeState, runtimeReducer, type TimelineEntry } from "./runtime";

const opened: OpenSessionResponse = {
  session_id: "session-1",
  thread_id: "agent",
  title: "session-1",
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
  pending_interactions: [],
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

  it("projects model and tool timing as soon as their events arrive", () => {
    let state = runtimeReducer(initialRuntimeState, { type: "opened", session: opened });
    state = runtimeReducer(state, { type: "event", event: event("turn_started", { turn: 1 }) });
    state = runtimeReducer(state, {
      type: "event",
      event: event("assistant_message", {
        content: "live",
        tool_calls: [],
        timing: { llm_ms: 100, ttft_ms: 25, decode_ms: 75 },
      }),
    });
    state = runtimeReducer(state, {
      type: "event",
      event: event("usage", { input_tokens: 10, output_tokens: 5, total_tokens: 15, context_tokens: 500 }),
    });
    state = runtimeReducer(state, {
      type: "event",
      event: event("tool_result", {
        tool_call_id: "call-1", name: "shell", content: "ok", status: "success",
        timing: { duration_ms: 40 },
      }),
    });

    expect(state.sessionStats).toMatchObject({
      turns: 1, steps: 1, llm_ms: 100, tool_ms: 40,
      ttft_ms: 25, ttft_steps: 1, decode_ms: 75, decode_tokens: 5,
    });
    expect(state.usage).toMatchObject({ total_tokens: 25, context_tokens: 500 });
  });

  it("keeps turn lifecycle events in the same ordered timeline as output", () => {
    let state = runtimeReducer(initialRuntimeState, { type: "opened", session: opened });
    state = runtimeReducer(state, {
      type: "events",
      events: [
        event("turn_started", { turn: 1 }),
        event("assistant_message", { content: "answer", tool_calls: [] }),
        event("turn_finished", { turn: 1 }),
      ],
    });

    expect(state.entries.map((entry) => entry.kind === "runtime" ? entry.event : entry.kind)).toEqual([
      "turn_started", "message", "turn_finished",
    ]);
  });

  it("keeps a newer live usage projection ahead of a stale thread refresh", () => {
    let state = runtimeReducer(initialRuntimeState, { type: "opened", session: opened });
    state = runtimeReducer(state, { type: "event", event: event("turn_started", { turn: 1 }) });
    state = runtimeReducer(state, {
      type: "event",
      event: event("usage", { input_tokens: 20, output_tokens: 5, total_tokens: 25, context_tokens: 700 }),
    });
    state = runtimeReducer(state, { type: "threads", threads: [{
      session_id: opened.session_id,
      title: opened.title,
      thread_id: opened.thread_id,
      status: "active",
      kind: "main",
      turn_status: "running",
      parent_thread_id: "",
      agent: opened.agent_name,
      provider: opened.provider,
      model: opened.model,
      model_mode: opened.model_mode,
      context_window: opened.context_window,
      message_count: 0,
      usage: { ...EMPTY_USAGE, input_tokens: 10, total_tokens: 10, context_tokens: 400 },
      session_stats: { ...EMPTY_SESSION_STATS },
      pending_interactions: [],
      status_slots: {},
    }] });

    expect(state.usage).toMatchObject({ input_tokens: 30, total_tokens: 35, context_tokens: 700 });
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

  it("keeps a live source-tagged message as runtime context", () => {
    let state = runtimeReducer(initialRuntimeState, { type: "opened", session: opened });
    const live = event("message", {
      id: "context-1",
      role: "user",
      content: "workspace facts",
      runtime: { source: "workspace", event: "instructions" },
    });
    state = runtimeReducer(state, { type: "event", event: live });
    state = runtimeReducer(state, { type: "event", event: live });

    expect(state.entries).toHaveLength(1);
    expect(state.entries[0]).toMatchObject({
      kind: "runtime",
      id: "runtime:context-1",
      source: "workspace",
      event: "instructions",
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

  it("keeps reasoning from a complete live assistant event when content is empty", () => {
    const state = runtimeReducer(initialRuntimeState, {
      type: "event",
      event: event("assistant_message", {
        id: "assistant-thinking-only",
        content: "",
        reasoning: "I am checking the tool result.",
        tool_calls: [],
      }),
    });

    expect(state.entries).toMatchObject([{
      kind: "message",
      role: "assistant",
      content: "",
      reasoning: "I am checking the tool result.",
      streaming: false,
    }]);
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
      expectedCursor: "20",
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
      expectedCursor: "10",
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

  it("projects accepted, claimed, and consumed delivery phases", () => {
    const item = {
      message_id: "queued-1", content: "continue later", target: "next-turn" as const,
      source: "user", image_count: 0, artifact_count: 0,
    };
    let state = runtimeReducer(initialRuntimeState, { type: "pending_inputs", items: [item] });
    expect(state.deliveryStates["queued-1"]).toBe("accepted");
    state = runtimeReducer(state, { type: "event", event: event("input_claimed", { message_ids: ["queued-1"] }) });
    expect(state.deliveryStates["queued-1"]).toBe("claimed");
    state = runtimeReducer(state, { type: "event", event: event("input_consumed", { message_ids: ["queued-1"] }) });
    expect(state.deliveryStates["queued-1"]).toBe("consumed");
    expect(state.pendingInputs).toEqual([]);
  });

  it("removes only the optimistic queue item when its request fails", () => {
    const state = runtimeReducer(
      runtimeReducer(initialRuntimeState, { type: "pending_inputs", items: [{
        message_id: "queued-1", content: "continue later", target: "next-turn",
        source: "user", image_count: 0, artifact_count: 0,
      }, {
        message_id: "queued-2", content: "keep this", target: "next-turn",
        source: "user", image_count: 0, artifact_count: 0,
      }] }),
      { type: "pending_input_failed", messageId: "queued-1" },
    );
    expect(state.pendingInputs.map((item) => item.message_id)).toEqual(["queued-2"]);
  });

  it("removes a steered input when its delivery message reaches the transcript", () => {
    let state = runtimeReducer(initialRuntimeState, { type: "pending_inputs", items: [{
      message_id: "steer-1", content: "interrupt with this", target: "next-step",
      source: "user", image_count: 0, artifact_count: 0,
    }] });
    state = runtimeReducer(state, {
      type: "event",
      event: event("message", { id: "steer-1", role: "user", content: "interrupt with this" }),
    });
    expect(state.pendingInputs).toEqual([]);
    expect(state.entries).toHaveLength(1);
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
      title: "Session title",
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
      session_id: "session-1", title: "Session title", thread_id: "agent", status: "active" as const,
      kind: "main" as const, turn_status: "running" as const, parent_thread_id: "",
      agent: "default", provider: "minimax", model: "MiniMax-M2", model_mode: "",
      context_window: 1000, message_count: 2, usage: opened.usage,
      session_stats: opened.session_stats, pending_interactions: [], status_slots: {},
    };

    expect(runtimeReducer(current, { type: "thread_synced", thread }).turnRunning).toBe(true);
  });

  it("marks a subagent thread view as read-only", () => {
    const current = runtimeReducer(initialRuntimeState, { type: "opened", session: opened });
    const subagent = {
      session_id: "session-1", title: "Reviewer", thread_id: "agent-reviewer-1", status: "active" as const,
      kind: "subagent" as const, turn_status: "idle" as const, parent_thread_id: "agent",
      agent: "reviewer", provider: "minimax", model: "MiniMax-M2", model_mode: "",
      context_window: 1000, message_count: 2, usage: opened.usage,
      session_stats: opened.session_stats, pending_interactions: [], status_slots: {},
    };
    const state = runtimeReducer(current, { type: "thread_synced", thread: subagent });
    expect(state.viewingSubagent).toBe(true);

    const main = { ...subagent, thread_id: "agent", kind: "main" as const, parent_thread_id: "" };
    expect(runtimeReducer(state, { type: "thread_synced", thread: main }).viewingSubagent).toBe(false);
    expect(runtimeReducer(initialRuntimeState, { type: "opened", session: opened }).viewingSubagent).toBe(false);
  });

  it("projects a viewed thread's live frames without touching the main entries", () => {
    const rolling = { reasoning: "", content: "" };
    let entries: TimelineEntry[] = [];
    entries = applyViewEvent(entries, event("assistant_message_delta", { content: "partial" }), rolling);
    expect(entries).toHaveLength(0);
    // The accumulated delta flushes at the turn boundary.
    entries = applyViewEvent(entries, event("turn_finished", { turn: 1 }), rolling);
    expect(entries).toHaveLength(2);
    expect(entries[0]).toMatchObject({ kind: "message", content: "partial", reasoning: "" });
    expect(entries[1]).toMatchObject({ kind: "runtime", content: "Turn 1 finished" });
    // A discrete assistant message appends its own entry.
    entries = applyViewEvent(entries, event("assistant_message", { content: "final answer" }), rolling);
    expect(entries).toHaveLength(3);
    expect(entries[2]).toMatchObject({ kind: "message", content: "final answer" });

    entries = applyViewEvent(entries, event("tool_result", { name: "shell", status: "success", content: "ok" }), rolling);
    expect(entries[3]).toMatchObject({ kind: "runtime" });
    expect((entries[3] as { content: string }).content).toContain("shell");
  });

  it("does not duplicate a replayed accepted input in a viewed thread", () => {
    const rolling = { reasoning: "", content: "" };
    let entries: TimelineEntry[] = [];
    entries = applyViewEvent(
      entries,
      event("message", { id: "sub-turn-1", role: "user", content: "worker task" }),
      rolling,
    );
    expect(entries).toHaveLength(1);

    // The observer replays the full thread event stream over the same
    // trajectory, so the same accepted input must not appear twice.
    entries = applyViewEvent(
      entries,
      event("message", { id: "sub-turn-1", role: "user", content: "worker task" }),
      rolling,
    );
    expect(entries).toHaveLength(1);
    expect(entries[0]).toMatchObject({ kind: "message", messageId: "sub-turn-1" });
  });

  it("streams reasoning and assistant content into one entry", () => {
    let state = runtimeReducer(initialRuntimeState, { type: "opened", session: opened });
    const committedEntries = state.entries;
    state = runtimeReducer(state, { type: "event", event: event("assistant_message_delta", { reasoning: "inspect " }) });
    expect(state.entries).toHaveLength(1);
    expect(state.entries[0]).toMatchObject({ role: "assistant", reasoning: "inspect ", streaming: true });
    state = runtimeReducer(state, { type: "event", event: event("assistant_message_delta", { content: "hello" }) });
    expect(state.entries).toHaveLength(1);
    expect(state.entries[0]).toMatchObject({ content: "hello", reasoning: "inspect ", streaming: true });
    state = runtimeReducer(state, { type: "event", event: event("assistant_message", { content: "hello", tool_calls: [] }) });

    expect(state.entries).toHaveLength(1);
    expect(state.entries[0]).toMatchObject({
      kind: "message",
      role: "assistant",
      content: "hello",
      reasoning: "inspect ",
      streaming: false,
    });
    expect(committedEntries).toEqual([]);
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

  it.each([
    ["turn_finished", "error"],
    ["turn_cancelled", "cancelled"],
  ] as const)("finalizes unanswered tools on %s and preserves terminal tools", (terminalType, expectedStatus) => {
    let state = runtimeReducer(initialRuntimeState, { type: "opened", session: opened });
    state = runtimeReducer(state, {
      type: "events",
      events: [
        event("turn_started", { turn: 1 }),
        event("tool_calls_started", {
          tool_calls: [
            { id: "pending", name: "shell", args: {} },
            { id: "running", name: "shell", args: {} },
          ],
        }),
        event("permission_request", {
          request_id: "approval-1",
          tool_call: { id: "approval", name: "shell", args: {} },
        }),
        event("tool_result", {
          tool_call_id: "completed", name: "shell", content: "ok", status: "success",
        }),
        event("tool_result", {
          tool_call_id: "cancelled", name: "shell", content: "", status: "cancelled",
        }),
        event(terminalType, { turn: 1, reason: terminalType === "turn_cancelled" ? "client_interrupt" : undefined }),
      ],
    });

    const tools = Object.fromEntries(
      state.entries
        .filter((entry): entry is Extract<TimelineEntry, { kind: "tool" }> => entry.kind === "tool")
        .map((entry) => [entry.toolCallId, entry]),
    );
    expect(Object.fromEntries(["pending", "running", "approval"].map((id) => [id, tools[id].status]))).toEqual({
      pending: expectedStatus,
      running: expectedStatus,
      approval: expectedStatus,
    });
    expect(tools.completed.status).toBe("success");
    expect(tools.cancelled.status).toBe("cancelled");
  });

  it("projects permission decisions onto the pending tool call", () => {
    let state = runtimeReducer(initialRuntimeState, { type: "opened", session: opened });
    state = runtimeReducer(state, {
      type: "event",
      event: event("permission_request", {
        request_id: "permission:call-1",
        tool_call: { id: "call-1", name: "shell", args: { command: "pwd" } },
      }),
    });
    expect(state.entries).toMatchObject([{
      kind: "tool",
      toolCallId: "call-1",
      status: "pending",
      permissionRequestId: "permission:call-1",
    }]);
    state = runtimeReducer(state, {
      type: "event",
      event: event("permission_response_recorded", {
        request_id: "permission:call-1",
        decision: "allow",
      }),
    });
    expect(state.entries[0]).toMatchObject({ status: "approved" });
    state = runtimeReducer(state, {
      type: "event",
      event: event("permission_denied", {
        request_id: "permission:call-1",
        reason: "sandbox",
      }),
    });
    expect(state.entries[0]).toMatchObject({ status: "denied" });
  });

  it("keeps compaction in the activity timeline", () => {
    let state = runtimeReducer(initialRuntimeState, { type: "opened", session: opened });
    state = runtimeReducer(state, {
      type: "events",
      events: [
        event("compaction_started", {}),
        event("compaction_completed", {}),
      ],
    });
    expect(state.entries.filter((entry) => entry.kind === "runtime").map((entry) => entry.event)).toEqual([
      "compaction_started", "compaction_completed",
    ]);
  });

  it("rebuilds context and one compact marker from durable trajectory", () => {
    const message = {
      role: "user" as const,
      content: "Injected instructions",
      tool_calls: [], tool_call_id: "", status: "", data: null,
      error: null, artifacts: [], images: [],
      runtime: { source: "skills", event: "inject" },
    };
    const state = runtimeReducer(initialRuntimeState, {
      type: "trajectory",
      nextCursor: null,
      items: [
        { position: 1, kind: "message", message_id: "context-1", message },
        { position: 2, kind: "event", event: "compaction/start", data: { compaction_id: "c1" }, timestamp: "2026-01-01T00:00:00Z" },
        { position: 3, kind: "event", event: "compaction/summary", data: { compaction_id: "c1", summary: "summary" }, timestamp: "2026-01-01T00:00:01Z" },
        { position: 4, kind: "surface_replace", operation: "compact:c1", transcript: "preserve", source_node_ids: ["1"], messages: [], summary: "Compacted summary" },
        { position: 5, kind: "event", event: "compaction/end", data: { compaction_id: "c1" }, timestamp: "2026-01-01T00:00:02Z" },
      ],
    });

    expect(state.entries).toHaveLength(2);
    expect(state.entries[0]).toMatchObject({ kind: "runtime", source: "skills", content: "Injected instructions" });
    expect(state.entries[1]).toMatchObject({ kind: "runtime", source: "compact", event: "compaction/end" });
  });

  it("replays the durable compaction summary instead of a placeholder", () => {
    const state = runtimeReducer(initialRuntimeState, {
      type: "trajectory",
      nextCursor: null,
      items: [
        { position: 1, kind: "event", event: "compaction/start", data: { compaction_id: "c9" }, timestamp: "2026-01-01T00:00:00Z" },
        {
          position: 2,
          kind: "surface_replace",
          operation: "compact:c9",
          transcript: "preserve",
          source_node_ids: ["0"],
          messages: [],
          summary: "What the session kept: billing migration decisions.",
        },
        { position: 3, kind: "event", event: "compaction/end", data: { compaction_id: "c9" }, timestamp: "2026-01-01T00:00:02Z" },
      ],
    });

    const compactEntries = state.entries.filter(
      (entry) => entry.kind === "runtime" && entry.source === "compact",
    );
    expect(compactEntries).toHaveLength(1);
    expect(compactEntries[0]).toMatchObject({
      content: "What the session kept: billing migration decisions.",
    });
  });

  it("reconciles assistant tool calls with results stored in later trajectory records", () => {
    const assistant = {
      id: "assistant-1",
      role: "assistant" as const,
      content: "",
      reasoning: "",
      tool_calls: [{ id: "call-1", name: "shell", args: { command: "printf hello" } }],
      tool_call_id: "", status: "", data: null, error: null, artifacts: [], images: [],
    };
    const result = {
      role: "tool" as const,
      content: "hello",
      tool_calls: [], tool_call_id: "call-1", status: "success", data: null,
      error: null, artifacts: [], images: [],
    };
    const state = runtimeReducer(initialRuntimeState, {
      type: "trajectory",
      nextCursor: null,
      items: [
        { position: 1, kind: "message", message_id: "assistant-1", message: assistant },
        { position: 2, kind: "message", message_id: "tool-1", message: result },
      ],
    });

    const tools = state.entries.filter((entry) => entry.kind === "tool");
    expect(tools).toHaveLength(1);
    expect(tools[0]).toMatchObject({
      toolCallId: "call-1",
      name: "shell",
      args: { command: "printf hello" },
      status: "success",
      result: "hello",
    });
  });

  it("does not duplicate a durable assistant message replayed by the live stream", () => {
    let state = runtimeReducer(initialRuntimeState, {
      type: "trajectory",
      nextCursor: null,
      items: [{
        position: 1,
        kind: "message",
        message_id: "assistant-1",
        message: {
          id: "assistant-1",
          role: "assistant",
          content: "finished while reconnecting",
          reasoning: "checked the reconnect path",
          tool_calls: [], tool_call_id: "", status: "", data: null,
          error: null, artifacts: [], images: [],
        },
      }],
    });
    state = runtimeReducer(state, {
      type: "event",
      event: event("assistant_message", {
        id: "assistant-1",
        content: "finished while reconnecting",
        tool_calls: [],
      }),
    });

    const messages = state.entries.filter((entry) => entry.kind === "message");
    expect(messages).toHaveLength(1);
    // The live event carries no reasoning; the durable one is preserved.
    expect(messages[0]).toMatchObject({ reasoning: "checked the reconnect path" });
  });

  it("keeps live entries while a newer trajectory baseline is applied", () => {
    let state = runtimeReducer(initialRuntimeState, {
      type: "trajectory",
      nextCursor: null,
      items: [{
        position: 1,
        kind: "message",
        message_id: "user-1",
        message: {
          id: "user-1", role: "user", content: "first",
          tool_calls: [], tool_call_id: "", status: "", data: null,
          error: null, artifacts: [], images: [],
        },
      }],
    });
    state = runtimeReducer(state, {
      type: "event",
      event: event("assistant_message", {
        id: "assistant-live", content: "arrived during refresh", tool_calls: [],
      }),
    });
    state = runtimeReducer(state, {
      type: "trajectory",
      nextCursor: null,
      items: [{
        position: 1,
        kind: "message",
        message_id: "user-1",
        message: {
          id: "user-1", role: "user", content: "first",
          tool_calls: [], tool_call_id: "", status: "", data: null,
          error: null, artifacts: [], images: [],
        },
      }],
    });
    const first = state.entries.find((entry) =>
      (entry.kind === "message" || entry.kind === "runtime" || entry.kind === "notice")
      && entry.content === "first"
    );
    expect(first?.origin).toBe("trajectory");
    expect(state.entries.some((entry) =>
      (entry.kind === "message" || entry.kind === "runtime" || entry.kind === "notice")
      && entry.content === "arrived during refresh"
    )).toBe(true);
  });

  it("does not duplicate an accepted user message when trajectory catches up", () => {
    let state = runtimeReducer(initialRuntimeState, {
      type: "trajectory",
      nextCursor: null,
      items: [{
        position: 1,
        kind: "message",
        message_id: "user-1",
        message: {
          id: "user-1", role: "user", content: "same input",
          tool_calls: [], tool_call_id: "", status: "", data: null,
          error: null, artifacts: [], images: [],
        },
      }],
    });
    state = runtimeReducer(state, {
      type: "event",
      event: event("message", {
        id: "user-1", role: "user", content: "same input", images: [], artifacts: [],
      }),
    });

    expect(state.entries.filter((entry) => entry.kind === "message")).toHaveLength(1);
  });

  it("folds raw events captured during the trajectory request after the baseline", () => {
    const state = runtimeReducer(initialRuntimeState, {
      type: "trajectory",
      nextCursor: "cursor-2",
      items: [{
        position: 1,
        kind: "message",
        message_id: "user-1",
        message: {
          id: "user-1", role: "user", content: "first",
          tool_calls: [], tool_call_id: "", status: "", data: null,
          error: null, artifacts: [], images: [],
        },
      }],
      bufferedEvents: [event("assistant_message", {
        id: "assistant-buffered", content: "arrived during baseline", tool_calls: [],
      })],
    });

    expect(state.entries.some((entry) => (
      (entry.kind === "message" || entry.kind === "runtime" || entry.kind === "notice")
      && entry.content === "arrived during baseline"
    ))).toBe(true);
  });

  it("reprojects a compact marker when history is replaced", () => {
    const state = runtimeReducer(initialRuntimeState, {
      type: "event",
      event: event("history_updated", {
        operation: "compact:automatic",
        history: [],
        history_cursor: "compact-1",
      }),
    });

    expect(state.entries).toMatchObject([{
      kind: "runtime",
      source: "compact",
      event: "compact:automatic",
      content: "Conversation history compacted",
      id: "event:1:history_updated",
    }]);
  });

  it("ignores an older page response after the history cursor advanced", () => {
    let state = runtimeReducer(initialRuntimeState, { type: "opened", session: {
      ...opened, history_cursor: "10",
    } });
    state = runtimeReducer(state, {
      type: "event",
      event: event("history_updated", { history: [], history_cursor: "20" }),
    });
    state = runtimeReducer(state, {
      type: "history_prepend",
      history: [{
        role: "user", content: "stale", tool_calls: [], tool_call_id: "", status: "",
        data: null, error: null, artifacts: [], images: [],
      }],
      nextCursor: null,
      expectedCursor: "10",
    });
    expect(state.entries).toEqual([]);
    expect(state.historyCursor).toBe("20");
  });

  it("reconciles provisional streamed tool ids with the executed call", () => {
    let state = runtimeReducer(initialRuntimeState, { type: "opened", session: opened });
    state = runtimeReducer(state, {
      type: "event",
      event: event("tool_call_delta", {
        tool_calls: [{
          tool_call_id: "tool_0",
          name: "shell",
          args_delta: '{"command":"pwd"}',
          index: 0,
        }],
      }),
    });
    state = runtimeReducer(state, {
      type: "event",
      event: event("tool_call_delta", {
        tool_calls: [{
          tool_call_id: "call_shell",
          replaces_tool_call_id: "tool_0",
          name: "shell",
          args_delta: "",
          index: 0,
        }],
      }),
    });
    state = runtimeReducer(state, {
      type: "event",
      event: event("tool_calls_started", {
        tool_calls: [{ id: "call_shell", name: "shell", args: { command: "pwd" } }],
      }),
    });
    state = runtimeReducer(state, {
      type: "event",
      event: event("tool_result", {
        tool_call_id: "call_shell",
        name: "shell",
        content: "/workspace",
        status: "success",
      }),
    });

    expect(state.entries).toHaveLength(1);
    expect(state.entries[0]).toMatchObject({
      kind: "tool",
      toolCallId: "call_shell",
      status: "success",
      result: "/workspace",
    });
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

  it("keeps streamed assistant text when a history page replaces the timeline", () => {
    let state = runtimeReducer(initialRuntimeState, { type: "opened", session: opened });
    state = runtimeReducer(state, { type: "events", events: [
      event("turn_started", { turn: 1 }),
      event("assistant_message_delta", { content: "FINAL ANSWER" }),
      event("history_updated", { operation: "compact:automatic", turns: 1, history: [] }),
    ] });

    expect(state.entries.some((entry) => entry.kind === "message" && entry.content === "FINAL ANSWER")).toBe(true);
  });

  it("keeps streamed assistant text when a durable trajectory is already loaded", () => {
    let state = runtimeReducer(initialRuntimeState, {
      type: "trajectory",
      nextCursor: null,
      items: [{
        position: 1,
        kind: "message",
        message_id: "user-1",
        message: {
          id: "user-1", role: "user", content: "question",
          tool_calls: [], tool_call_id: "", status: "", data: null,
          error: null, artifacts: [], images: [],
        },
      }],
    });
    state = runtimeReducer(state, { type: "events", events: [
      event("assistant_message_delta", { content: "X" }),
      event("history_updated", { operation: "compact:automatic", turns: 1, history: [] }),
    ] });

    expect(state.entries.some((entry) => entry.kind === "message" && entry.content === "X")).toBe(true);
  });

  it("keeps both bodies when two server messages reuse one id", () => {
    const state = runtimeReducer(initialRuntimeState, {
      type: "events",
      events: [
        event("turn_started", { turn: 7 }),
        event("assistant_message_delta", { content: "step answer " }),
        event("assistant_message", {
          id: "assistant-7-200",
          content: "step answer",
          tool_calls: [{ id: "call-1", name: "shell", args: { command: "pwd" } }],
        }),
        event("tool_calls_started", { tool_calls: [{ id: "call-1", name: "shell", args: { command: "pwd" } }] }),
        event("tool_result", { tool_call_id: "call-1", name: "shell", content: "ok", status: "success" }),
        event("assistant_message_delta", { content: "# Final Answer\n\nAll done." }),
        event("assistant_message", { id: "assistant-7-200", content: "# Final Answer\n\nAll done.", tool_calls: [] }),
        event("turn_finished", { turn: 7 }),
      ],
    });

    const texts = state.entries
      .filter((entry) => entry.kind === "message" && entry.role === "assistant")
      .map((entry) => (entry.kind === "message" ? entry.content : ""));
    expect(texts).toContain("step answer");
    expect(texts).toContain("# Final Answer\n\nAll done.");
  });

  it("renders one entry for an idempotent replay of one assistant message", () => {
    const state = runtimeReducer(initialRuntimeState, {
      type: "events",
      events: [
        event("assistant_message", { id: "assistant-1-1-abcd", content: "same", tool_calls: [] }),
        event("assistant_message", { id: "assistant-1-1-abcd", content: "same", tool_calls: [] }),
      ],
    });

    expect(state.entries.filter((entry) => entry.kind === "message")).toHaveLength(1);
    expect(state.entries[0]).toMatchObject({ content: "same" });
  });

  it("keeps in-flight streamed text when a trajectory baseline arrives", () => {
    let state = runtimeReducer(initialRuntimeState, { type: "opened", session: opened });
    state = runtimeReducer(state, { type: "event", event: event("assistant_message_delta", { content: "half a sen" }) });
    state = runtimeReducer(state, {
      type: "trajectory",
      nextCursor: null,
      items: [{
        position: 1,
        kind: "message",
        message_id: "user-1",
        message: {
          id: "user-1", role: "user", content: "question",
          tool_calls: [], tool_call_id: "", status: "", data: null,
          error: null, artifacts: [], images: [],
        },
      }],
      bufferedEvents: [event("assistant_message_delta", { content: "tence." })],
    });

    expect(state.entries.some((entry) => (
      entry.kind === "message" && entry.content === "half a sentence."
    ))).toBe(true);
  });

  it("restores unanswered interactions from an open response", () => {
    const state = runtimeReducer(initialRuntimeState, {
      type: "opened",
      session: {
        ...opened,
        pending_interactions: [
          {
            type: "permission_request",
            data: {
              request_id: "permission-1",
              source: "permission_system",
              reason: "write file",
              tool_call: { id: "call-1", name: "filesystem_write", args: { path: "a.txt" } },
              resume_supported: true,
            },
          },
          {
            type: "user_input_required",
            data: {
              request_id: "input-1",
              source: "ask_user",
              tool_call_id: "call-2",
              question: "Choose one",
              options: [
                { label: "A", description: "First" },
                { label: "B", description: "Second" },
              ],
            },
          },
          { type: "unrelated_event", data: { request_id: "ignored" } },
        ],
      },
    });

    expect(state.interactions.map((item) => item.request_id)).toEqual(["permission-1", "input-1"]);
    expect(state.interactions[0]).toMatchObject({ kind: "permission", tool_call: { id: "call-1" } });
  });

  it("projects live streaming and durable history in the same order", () => {
    const historyItem = (role: "user" | "assistant" | "tool", extra: Record<string, unknown> = {}) => ({
      role,
      content: "",
      tool_calls: [],
      tool_call_id: "",
      status: "",
      data: null,
      error: null,
      artifacts: [],
      images: [],
      ...extra,
    });
    const history = [
      historyItem("user", { id: "user-1", content: "question" }),
      historyItem("assistant", {
        id: "assistant-1-1-abcd",
        content: "step answer",
        tool_calls: [{ id: "call-1", name: "shell", args: { command: "pwd" } }],
      }),
      historyItem("tool", { tool_call_id: "call-1", content: "ok", status: "success" }),
      historyItem("assistant", { id: "assistant-1-2-efgh", content: "final answer" }),
    ];
    const state = runtimeReducer(initialRuntimeState, {
      type: "events",
      events: [
        event("turn_started", { turn: 1 }),
        event("message", { id: "user-1", role: "user", content: "question" }),
        event("assistant_message_delta", { reasoning: "thinking " }),
        event("assistant_message_delta", { content: "step answer" }),
        event("tool_call_delta", { tool_calls: [{ tool_call_id: "call-1", name: "shell", args_delta: '{"command":"pwd"}' }] }),
        event("assistant_message", {
          id: "assistant-1-1-abcd",
          content: "step answer",
          tool_calls: [{ id: "call-1", name: "shell", args: { command: "pwd" } }],
        }),
        event("tool_calls_started", { tool_calls: [{ id: "call-1", name: "shell", args: { command: "pwd" } }] }),
        event("tool_result", { tool_call_id: "call-1", name: "shell", content: "ok", status: "success" }),
        event("assistant_message_delta", { content: "final answer" }),
        event("assistant_message", { id: "assistant-1-2-efgh", content: "final answer", tool_calls: [] }),
        event("turn_finished", { turn: 1 }),
      ],
    });

    expect(visibleProjection(state.entries)).toEqual(visibleProjection(historyEntries(history)));
  });
});

function visibleProjection(entries: ReturnType<typeof historyEntries>): string[] {
  return entries
    .filter((entry) => entry.kind === "message" || entry.kind === "tool")
    .map((entry) => (entry.kind === "message"
      ? `message:${entry.role}:${entry.content}`
      : `tool:${entry.toolCallId}`));
}
