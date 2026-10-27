/** Values exposed by the session and agent-loop HTTP APIs. */
import type { ArtifactRef, JsonObject } from "./types";

export interface HistoryPage<T> {
  items: T[];
  older_cursor: string | null;
}

export interface ModelTiming {
  total_ms: number;
  first_delta_ms: number | null;
}

export interface ToolCall {
  id: string;
  name: string;
  args: JsonObject;
  type: "tool_call";
}

export type MediaPart = { kind: "media"; artifact: ArtifactRef };
export type ToolOutput = {
  parts: ({ kind: "text"; text: string } | MediaPart)[];
  structured: JsonObject | null;
  artifacts: ArtifactRef[];
};
export type ToolOutcome =
  | { kind: "succeeded"; output: ToolOutput }
  | { kind: "failed"; error: JsonObject; output: ToolOutput }
  | { kind: "denied" | "cancelled"; reason: string };

export type ConversationRecord =
  | { kind: "human_input"; id: string; content: string; media: ArtifactRef[]; artifacts: ArtifactRef[] }
  | { kind: "runtime_notice"; id: string; source: string; event: string; content: string; media: ArtifactRef[]; artifacts: ArtifactRef[] }
  | { kind: "assistant"; id: string; content: string; reasoning: string; tool_calls: ToolCall[]; timing: ModelTiming; stop: { kind: "completed" | "length_limited" | "tool_calls_requested" } }
  | { kind: "tool"; id: string; call: { id: string; name: string }; outcome: ToolOutcome; timing: { duration_ms: number } }
  | { kind: "compaction_summary"; id: string; summary: string };

export interface TokenCounters {
  input: number;
  output: number;
  cache_read: number;
  cache_create: number;
  prompt_cache_write: number;
}
export interface UsageSnapshot {
  total_counters: TokenCounters;
  latest_turn_observation: {
    estimated_input_tokens: number;
    observed_context: { kind: "provider_measured"; tokens: number } | { kind: "measurement_unavailable"; reason: string };
  } | null;
}
export interface OpenedThread {
  key: { session_id: string; thread_id: string };
  metadata: {
    title: string;
    workspace_root: string;
    parent_thread_id: string;
    runtime_selection: {
      agent_name: string;
      model: {
        route: { provider: string; model: string };
        generation: { mode: { kind: "standard" } | { kind: "reasoning"; effort: string } };
        context_window: number;
      };
    };
  };
  usage: UsageSnapshot;
  status_slots: Record<string, string>;
  event_cursor: number;
  history: HistoryPage<ConversationRecord>;
  pending_inputs: import("./types").PendingInput[];
  pending_interactions: JsonObject[];
}
export interface ServerEvent {
  protocol_version: string;
  session_id: string;
  thread_id: string;
  sequence: number;
  scope: { kind: "session" } | { kind: "turn"; turn_id: string };
  kind: string;
  payload: JsonObject;
}

export type ConversationMessage =
  | { kind: "human_input" | "runtime_notice"; id: string; source?: string; event?: string; parts: ({ kind: "text"; text: string } | MediaPart)[]; artifacts: ArtifactRef[] }
  | { kind: "assistant"; id: string; parts: ({ kind: "text" | "reasoning"; text: string } | ToolCall)[]; exchange: { timing: ModelTiming; stop: { kind: "completed" | "length_limited" | "tool_calls_requested" } } }
  | Extract<ConversationRecord, { kind: "tool" | "compaction_summary" }>;
export type TrajectoryEntry =
  | { kind: "message_appended"; position: number; message: ConversationMessage }
  | { kind: "surface_replaced"; position: number; operation: string; transcript_policy: "preserve" | "replace"; source_ids: string[]; replacements: ConversationMessage[] }
  | { kind: "durable_event_recorded"; position: number; timestamp: string; event: JsonObject };
export interface TrajectoryRead {
  page: HistoryPage<TrajectoryEntry>;
  newest_position: number;
}

export interface HistoryMutation {
  removed_turns: number;
  history: HistoryPage<ConversationRecord>;
  stats: import("./types").SessionStatsData;
}
export interface TaskList {
  version: number;
  next_id: number;
  tasks: { id: string; subject: string; description: string; activeForm: string; owner: string; status: "pending" | "in_progress" | "completed" }[];
  edges: { prerequisite: string; dependent: string }[];
  completed_prerequisites: string[];
  completed_count: number;
}
