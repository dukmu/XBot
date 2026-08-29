export const PROTOCOL_VERSION = "xbotv2.v3";

export type JsonObject = Record<string, unknown>;

export interface ImageInput {
  data: string;
  media_type: string;
}

export interface AttachmentInput extends ImageInput {
  name: string;
}

export interface ImageReference {
  path: string;
  media_type: string;
  size: number;
  url?: string;
}

export interface UsageData {
  input_tokens: number;
  output_tokens: number;
  total_tokens: number;
  requests: number;
  context_tokens: number;
  cache_read_input_tokens: number;
  cache_creation_input_tokens: number;
  prompt_cache_write_tokens: number;
}

export interface SessionSummary {
  session_id: string;
  status: "active" | "inactive";
  active_threads: number;
  thread_count: number;
  workspace_root?: string;
  title?: string;
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
  workspace_root?: string;
  title?: string;
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
  images: ImageReference[];
  runtime?: Record<string, string> | null;
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
  history_cursor?: string | null;
  status_slots: Record<string, string>;
}

export interface MessagePage {
  messages: HistoryItem[];
  next_cursor: string | null;
}

export interface ProviderInfo {
  name: string;
  provider: string;
  default_model: string;
  models: ModelInfo[];
}

export interface ModelInfo {
  model: string;
  max_context_tokens: number;
  max_output_tokens: number | null;
  reasoning_effort: string;
  effort: string[];
  thinking: string;
  input_modalities: ("text" | "image")[];
}

export interface AgentInfo {
  name: string;
  description: string;
  mode: "primary" | "subagent" | "all";
  provider: string;
  model: string;
  context_window: number;
}

export interface CommandInfo {
  name: string;
  slash: string;
  kind: "client" | "server" | "prompt";
  description: string;
  usage: string;
  examples: string[];
  parameters: Record<string, unknown>;
}

export type CommandEffect = "history" | "thread" | "agents" | "tasks" | "commands" | "sessions";

export interface CommandResultData {
  command: string;
  status: "ok" | "error";
  message: string;
  effects: CommandEffect[];
}

export interface CommandResult {
  type: "command_result";
  data: CommandResultData;
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

export interface TodoItemData {
  content: string;
  status: "pending" | "in_progress" | "completed";
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
  cache_read_input_tokens: 0,
  cache_creation_input_tokens: 0,
  prompt_cache_write_tokens: 0,
};
