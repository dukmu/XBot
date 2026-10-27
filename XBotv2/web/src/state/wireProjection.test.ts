import { describe, expect, it } from "vitest";
import { projectEvent, projectOpened, projectTrajectory, projectUsage } from "./wireProjection";
import type { OpenedThread, ServerEvent, TrajectoryRead } from "../api/wire";

const counters = { input: 10, output: 4, cache_read: 2, cache_create: 1, prompt_cache_write: 0 };
const snapshot: OpenedThread = {
  key: { session_id: "s", thread_id: "main" },
  metadata: { title: "Example", workspace_root: "/tmp", parent_thread_id: "", runtime_selection: { agent_name: "default", model: { route: { provider: "p", model: "m" }, generation: { mode: { kind: "reasoning", effort: "high" } }, context_window: 4096 } } },
  usage: { total_counters: counters, latest_turn_observation: { estimated_input_tokens: 9, observed_context: { kind: "provider_measured", tokens: 8 } } },
  status_slots: {}, event_cursor: 31, history: { older_cursor: "older", items: [{ kind: "human_input", id: "u", content: "hello", media: [], artifacts: [] }] },
  pending_inputs: [], pending_interactions: [{ kind: "permission_request", interaction_id: "i", source: "tool", reason: "approve", subject: { kind: "named", tool: "shell", params: { command: "pwd" } }, resume_supported: true }],
};
const frame = (kind: string, payload: Record<string, unknown>): ServerEvent => ({ protocol_version: "xbotv2.v3", session_id: "s", thread_id: "main", sequence: 32, scope: { kind: "turn", turn_id: "u" }, kind, payload });

describe("production wire projection", () => {
  it("uses snapshot identity, selection, usage, cursor and pending interactions", () => {
    const view = projectOpened(snapshot, { turn_status: "running", session_stats: { turns: 3, steps: 4, llm_ms: 5, tool_ms: 0, ttft_ms: 0, ttft_steps: 0, decode_ms: 0, decode_tokens: 0 } }, (id) => `/artifact/${id}`);
    expect(view).toMatchObject({ session_id: "s", thread_id: "main", event_cursor: 31, history_cursor: "older", turn_status: "running", session_stats: { turns: 3, steps: 4 }, model_mode: "high", usage: { input_tokens: 10, output_tokens: 4, context_tokens: 8 }, history: [{ role: "user", content: "hello" }], pending_interactions: [{ data: { request_id: "i", tool_call: { name: "shell", args: { command: "pwd" } } } }] });
  });
  it("marks estimated context when the provider did not measure it", () => {
    expect(projectUsage({ total_counters: counters, latest_turn_observation: { estimated_input_tokens: 12, observed_context: { kind: "measurement_unavailable", reason: "not supplied" } } })).toMatchObject({ context_tokens: 12, context_estimated: true });
  });
  it("projects typed stream deltas, tool outcomes and history mutation", () => {
    expect(projectEvent(frame("assistant_text_delta", { text: "Hi" }), String)).toMatchObject({ type: "assistant_message_delta", data: { content: "Hi" } });
    expect(projectEvent(frame("turn_ended", { turn: 1, outcome: { kind: "cancelled", reason: "stop" } }), String).type).toBe("turn_cancelled");
    expect(projectEvent(frame("history_updated", { operation: "undo", mutation: { removed_turns: 1, history: snapshot.history, stats: { turns: 0 } } }), String).data).toMatchObject({ history_cursor: "older", history: [{ content: "hello" }] });
    expect(projectEvent(frame("job_updated", { view: { id: "j", kind: "shell", label: "wait", state: "running", elapsed_ms: 1, summary: null } }), String).data).toMatchObject({ id: "j", state: "running" });
  });
  it("projects canonical trajectory messages and page cursor", () => {
    const read: TrajectoryRead = { newest_position: 9, page: { older_cursor: "older", items: [{ kind: "message_appended", position: 3, message: { kind: "human_input", id: "u", parts: [{ kind: "text", text: "hello" }], artifacts: [] } }] } };
    expect(projectTrajectory(read, String)).toMatchObject({ newest_position: 9, next_cursor: "older", items: [{ kind: "message", message: { content: "hello" } }] });
  });
});
