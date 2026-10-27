/** Translate transport records into the existing presentation model. */
import type { HistoryViewItem, JsonObject, UsageData } from "../api/types";
import type { ConversationRecord, UsageSnapshot } from "../api/wire";

export function projectUsage(snapshot: UsageSnapshot): UsageData {
  const counters = snapshot.total_counters;
  const observed = snapshot.latest_turn_observation?.observed_context;
  return {
    input_tokens: counters.input,
    output_tokens: counters.output,
    total_tokens: counters.input + counters.output,
    context_tokens: observed?.kind === "provider_measured" ? observed.tokens : snapshot.latest_turn_observation?.estimated_input_tokens ?? 0,
    context_estimated: observed?.kind === "measurement_unavailable",
    cache_read_input_tokens: counters.cache_read,
    cache_creation_input_tokens: counters.cache_create,
    prompt_cache_write_tokens: counters.prompt_cache_write,
  };
}

export function projectRecord(record: ConversationRecord): HistoryViewItem {
  const empty = {
    tool_calls: [], tool_call_id: "", status: "", data: null,
    error: null, artifacts: [], media: [],
  };
  switch (record.kind) {
    case "human_input":
      return { ...empty, role: "user", id: record.id, content: record.content, media: record.media, artifacts: record.artifacts as unknown as JsonObject[] };
    case "runtime_notice":
      return { ...empty, role: "user", id: record.id, content: record.content, media: record.media, artifacts: record.artifacts as unknown as JsonObject[], runtime: { source: record.source, event: record.event } };
    case "assistant":
      return { ...empty, role: "assistant", id: record.id, content: record.content, reasoning: record.reasoning, tool_calls: record.tool_calls as unknown as JsonObject[], timing: { llm_ms: record.timing.total_ms, ttft_ms: record.timing.first_delta_ms ?? 0 } };
    case "tool": {
      const output = "output" in record.outcome ? record.outcome.output : null;
      const content = output?.parts.filter((part) => part.kind === "text").map((part) => part.text).join("") ?? ("reason" in record.outcome ? record.outcome.reason : "");
      return { ...empty, role: "tool", id: record.id, content, tool_call_id: record.call.id, status: record.outcome.kind === "succeeded" ? "success" : record.outcome.kind, data: output?.structured ?? null, error: record.outcome.kind === "failed" ? record.outcome.error : null, artifacts: output?.artifacts as unknown as JsonObject[] ?? [], outcome: record.outcome as unknown as JsonObject };
    }
    case "compaction_summary":
      return { ...empty, role: "user", id: record.id, content: record.summary, runtime: { source: "compact", event: "summary" } };
  }
}

function permissionCall(request: JsonObject): JsonObject {
  const subject = request.subject as JsonObject;
  return subject.kind === "tool" ? subject.tool_call as JsonObject : { id: "", name: subject.tool, args: subject.params };
}

export function projectOpened(opened: import("../api/wire").OpenedThread, summary: Pick<import("../api/types").ThreadSummary, "session_stats" | "turn_status">, artifactUrl: (id: string) => string): import("../api/types").SessionView {
  const selection = opened.metadata.runtime_selection;
  const mode = selection.model.generation.mode;
  return {
    session_id: opened.key.session_id,
    thread_id: opened.key.thread_id,
    title: opened.metadata.title,
    status: "ready",
    turn_status: summary.turn_status,
    agent_name: selection.agent_name,
    workspace_root: opened.metadata.workspace_root,
    provider: selection.model.route.provider,
    model: selection.model.route.model,
    model_mode: mode.kind === "reasoning" ? mode.effort : "",
    context_window: selection.model.context_window,
    usage: projectUsage(opened.usage),
    session_stats: summary.session_stats,
    history: opened.history.items.map((record) => decorateView(projectRecord(record), artifactUrl)),
    history_cursor: opened.history.older_cursor,
    event_cursor: opened.event_cursor,
    status_slots: opened.status_slots,
    pending_inputs: opened.pending_inputs,
    pending_interactions: opened.pending_interactions.map((request) => ({ type: String(request.kind), data: { ...request, request_id: request.interaction_id, tool_call: request.kind === "permission_request" ? permissionCall(request) : undefined } })),
  };
}

export function projectMessage(message: import("../api/wire").ConversationMessage): HistoryViewItem {
  if (message.kind === "tool" || message.kind === "compaction_summary") return projectRecord(message);
  if (message.kind === "assistant") {
    return projectRecord({
      kind: "assistant", id: message.id,
      content: message.parts.filter((part) => "kind" in part && part.kind === "text").map((part) => "text" in part ? part.text : "").join(""),
      reasoning: message.parts.filter((part) => "kind" in part && part.kind === "reasoning").map((part) => "text" in part ? part.text : "").join(""),
      tool_calls: message.parts.filter((part): part is import("../api/wire").ToolCall => "type" in part && part.type === "tool_call"),
      timing: message.exchange.timing, stop: message.exchange.stop,
    });
  }
  const content = message.parts.filter((part) => "kind" in part && part.kind === "text").map((part) => "text" in part ? part.text : "").join("");
  const media = message.parts.filter((part): part is import("../api/wire").MediaPart => part.kind === "media").map((part) => part.artifact);
  return projectRecord(message.kind === "human_input"
    ? { kind: "human_input", id: message.id, content, media, artifacts: message.artifacts }
    : { kind: "runtime_notice", id: message.id, source: message.source ?? "", event: message.event ?? "", content, media, artifacts: message.artifacts });
}

function decorateView(item: HistoryViewItem, artifactUrl: (id: string) => string): HistoryViewItem {
  const output = item.outcome?.output as JsonObject | undefined;
  const parts = output?.parts as JsonObject[] | undefined;
  const outcome = output && parts ? {
    ...item.outcome,
    output: { ...output, parts: parts.map((part) => {
      if (part.kind !== "media") return part;
      const artifact = part.artifact as JsonObject;
      return { ...part, artifact: { ...artifact, url: artifactUrl(String(artifact.id)) } };
    }) },
  } : item.outcome;
  return {
    ...item, outcome,
    media: item.media?.map((ref) => ({ ...ref, url: artifactUrl(ref.id) })),
    artifacts: item.artifacts.map((ref) => ({ ...ref, url: artifactUrl(String(ref.id)) })),
  };
}

export function projectTrajectory(read: import("../api/wire").TrajectoryRead, artifactUrl: (id: string) => string): import("../api/types").TrajectoryPage {
  const positions = new Map<string, string>();
  const items = read.page.items.map((entry): import("../api/types").TrajectoryItem => {
    if (entry.kind === "message_appended") {
      positions.set(entry.message.id, String(entry.position));
      return { kind: "message", position: entry.position, message_id: entry.message.id, message: decorateView(projectMessage(entry.message), artifactUrl) };
    }
    if (entry.kind === "surface_replaced") {
      return { kind: "surface_replace", position: entry.position, operation: entry.operation, transcript: entry.transcript_policy, source_node_ids: entry.source_ids.map((id) => positions.get(id) ?? id), messages: entry.replacements.map((message) => decorateView(projectMessage(message), artifactUrl)), summary: "" };
    }
    return { kind: "event", position: entry.position, timestamp: entry.timestamp, event: String(entry.event.kind), data: entry.event };
  });
  return { items, next_cursor: read.page.older_cursor, newest_position: read.newest_position };
}

export function projectEvent(event: import("../api/wire").ServerEvent, artifactUrl: (id: string) => string): import("../api/types").ViewEvent {
  let type = event.kind;
  let data: JsonObject = event.payload;
  switch (event.kind) {
    case "agent_configured": {
      const selection = event.payload.runtime_selection as import("../api/wire").OpenedThread["metadata"]["runtime_selection"];
      const mode = selection.model.generation.mode;
      data = { agent_name: selection.agent_name, provider: selection.model.route.provider, model: selection.model.route.model, model_mode: mode.kind === "reasoning" ? mode.effort : "", context_window: selection.model.context_window };
      break;
    }
    case "message": {
      const item = decorateView(projectRecord(event.payload as unknown as ConversationRecord), artifactUrl);
      data = { ...item, id: item.id, role: item.role };
      break;
    }
    case "assistant_text_delta":
      type = "assistant_message_delta"; data = { content: event.payload.text }; break;
    case "assistant_reasoning_delta":
      type = "assistant_message_delta"; data = { reasoning: event.payload.text }; break;
    case "assistant_completed": {
      type = "assistant_message";
      const item = projectRecord(event.payload as unknown as ConversationRecord);
      const stop = event.payload.stop as JsonObject;
      data = { ...item, stop_reason: stop.kind === "length_limited" ? "length" : stop.kind };
      break;
    }
    case "tool_calls_started":
      data = { tool_calls: (event.payload.calls as { call: JsonObject }[]).map((item) => item.call) }; break;
    case "tool_call_delta":
      data = { tool_calls: [{ tool_call_id: event.payload.call_id, name: event.payload.name_delta, args_delta: event.payload.arguments_delta }] }; break;
    case "tool_completed": {
      type = "tool_result";
      const item = decorateView(projectRecord(event.payload as unknown as ConversationRecord), artifactUrl);
      data = { ...item, name: (event.payload.call as JsonObject).name, timing: event.payload.timing };
      break;
    }
    case "task_changed":
      type = "todo_updated"; data = event.payload.snapshot as JsonObject; break;
    case "goal_changed":
      type = "goal_updated"; data = (event.payload.snapshot as JsonObject).state as JsonObject; break;
    case "job_updated": case "job_completed":
      type = "job_updated"; data = event.payload.view as JsonObject; break;
    case "turn_ended":
      type = (event.payload.outcome as JsonObject).kind === "cancelled" ? "turn_cancelled" : "turn_finished"; break;
    case "usage_updated":
      type = "usage_snapshot"; data = { usage: projectUsage(event.payload.snapshot as unknown as UsageSnapshot) }; break;
    case "permission_request": {
      data = { ...event.payload, request_id: event.payload.interaction_id, tool_call: permissionCall(event.payload) };
      break;
    }
    case "user_input_required": case "permission_response_recorded": case "user_input_recorded":
      data = { ...event.payload, request_id: event.payload.interaction_id, decision: (event.payload.approval as JsonObject | undefined)?.kind === "denied" ? "deny" : "allow" }; break;
    case "history_updated": {
      const mutation = event.payload.mutation as import("../api/wire").HistoryMutation;
      data = { operation: event.payload.operation, history: mutation.history.items.map((record) => decorateView(projectRecord(record), artifactUrl)), history_cursor: mutation.history.older_cursor, session_stats: mutation.stats };
      break;
    }
  }
  return { protocol_version: event.protocol_version, session_id: event.session_id, thread_id: event.thread_id, sequence: event.sequence, request_id: event.scope.kind === "turn" ? event.scope.turn_id : "", type, data };
}
