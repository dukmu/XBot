export const PROTOCOL_VERSION = "xbotv2.v3";

export type JsonObject = Record<string, unknown>;

export interface UsageData {
  input_tokens: number;
  output_tokens: number;
  total_tokens: number;
  requests: number;
  context_tokens: number;
}

export interface SessionSummary {
  session_id: string;
  status: "active" | "inactive";
  active_threads: number;
  thread_count: number;
}

export interface ThreadSummary {
  session_id: string;
  thread_id: string;
  status: "active" | "inactive";
  kind: "main" | "subagent";
  turn_status: "idle" | "running";
  parent_thread_id: string;
  agent: string;
  provider: string;
  model: string;
  model_mode: string;
  context_window: number;
  message_count: number;
  usage: UsageData;
  pending_interactions: string[];
  status_slots: Record<string, string>;
}

export interface HistoryItem {
  role: "user" | "assistant" | "tool";
  content: string;
  tool_calls: JsonObject[];
  tool_call_id: string;
  status: string;
  data: unknown;
  error: JsonObject | null;
  artifacts: JsonObject[];
}

export interface OpenSessionResponse {
  session_id: string;
  thread_id: string;
  status: "ready";
  agent_name: string;
  workspace_root: string;
  provider: string;
  model: string;
  model_mode: string;
  context_window: number;
  usage: UsageData;
  history: HistoryItem[];
  status_slots: Record<string, string>;
}

export interface ProviderInfo {
  name: string;
  provider: string;
  model: string;
  max_tokens: number;
  reasoning_effort: string;
  thinking_enabled: boolean;
}

export interface AgentInfo {
  name: string;
  description: string;
  mode: "primary" | "subagent" | "all";
  provider: string;
  model: string;
  context_window: number;
}

export interface TaskData {
  task_id: string;
  kind: "shell" | "agent";
  command: string;
  cwd: string;
  status: "pending" | "running" | "completed" | "failed" | "stopped";
  created_at: number;
  started_at: number;
  finished_at: number;
  output: string;
  error: string;
  agent: string;
  thread_id: string;
  usage: Record<string, unknown>;
}

export interface ToolCall {
  id: string;
  name: string;
  args: JsonObject;
  type?: string;
}

export interface UserInputOption {
  label: string;
  description: string;
}

export interface PermissionRequest {
  kind: "permission";
  request_id: string;
  source: string;
  reason: string;
  tool_call: ToolCall;
  resume_supported: boolean;
}

export interface UserInputRequest {
  kind: "user_input";
  request_id: string;
  source: string;
  tool_call_id: string;
  question: string;
  options: UserInputOption[];
  timeout_seconds?: number;
  resume_supported: boolean;
}

export type InteractionRequest = PermissionRequest | UserInputRequest;

export interface ServerEvent {
  protocol_version: string;
  session_id: string;
  thread_id: string;
  request_id: string;
  sequence: number;
  type: string;
  data: JsonObject;
}

export interface XBotErrorBody {
  code: string;
  message: string;
  details?: JsonObject;
  retryable?: boolean;
}

export const EMPTY_USAGE: UsageData = {
  input_tokens: 0,
  output_tokens: 0,
  total_tokens: 0,
  requests: 0,
  context_tokens: 0,
};
