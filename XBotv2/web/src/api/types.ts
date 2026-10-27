export const PROTOCOL_VERSION = "xbotv2.v3";

export type JsonObject = Record<string, unknown>;

export interface MediaInput {
  data: string;
  media_type: string;
  name: string;
}

export interface AttachmentInput extends MediaInput {
  name: string;
}

export interface MessageRequest {
  content: string;
  request_id: string;
  delivery: "queue" | "steer";
  media: MediaInput[];
  attachments: AttachmentInput[];
}

export interface RegenerateRequest {
  request_id: string;
}

export interface ArtifactRef {
  id: string;
  media_type: string;
  name: string;
  kind: string;
  size: number;
  sha256: string;
  url?: string;
}

export interface UsageData {
  input_tokens: number;
  output_tokens: number;
  total_tokens: number;
  context_tokens: number;
  context_estimated?: boolean;
  cache_read_input_tokens: number;
  cache_creation_input_tokens: number;
  prompt_cache_write_tokens: number;
}

export interface SessionStatsData {
  turns: number;
  steps: number;
  llm_ms: number;
  tool_ms: number;
  ttft_ms: number;
  ttft_steps: number;
  decode_ms: number;
  decode_tokens: number;
}

export interface SessionSummary {
  session_id: string;
  status: "active" | "inactive";
  active_threads: number;
  thread_count: number;
  blank: boolean;
  workspace_root?: string;
  title: string;
}

export interface SessionListData {
  sessions: SessionSummary[];
  event_cursor: number;
}

export interface WorkspaceData {
  workspace_id: string;
  path: string;
  title: string;
  session_ids: string[];
  created_at: string;
  updated_at: string;
}

export interface WorkspaceListData {
  items: WorkspaceData[];
  archived_session_ids: string[];
  event_cursor: number;
}

export interface DirectoryEntryData {
  name: string;
  path: string;
  hidden: boolean;
}

export interface DirectoryListingData {
  path: string;
  parent: string | null;
  home: string;
  separator: "/" | "\\";
  entries: DirectoryEntryData[];
  truncated: boolean;
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
  usage: import("./wire").UsageSnapshot;
  session_stats: SessionStatsData;
  pending_interactions: string[];
  status_slots: Record<string, string>;
  workspace_root?: string;
  title: string;
}

export interface HistoryViewItem {
  role: "user" | "assistant" | "tool";
  id?: string;
  content: string;
  reasoning?: string;
  tool_calls: JsonObject[];
  tool_call_id: string;
  status: string;
  data: unknown;
  error: JsonObject | null;
  artifacts: JsonObject[];
  media?: ArtifactRef[];
  outcome?: JsonObject;
  runtime?: Record<string, string> | null;
  timing?: Record<string, number> | null;
}

export interface SessionView {
  session_id: string;
  thread_id: string;
  title: string;
  status: "ready";
  turn_status: "idle" | "running";
  agent_name: string;
  workspace_root: string;
  provider: string;
  model: string;
  model_mode: string;
  context_window: number;
  usage: UsageData;
  session_stats: SessionStatsData;
  history: HistoryViewItem[];
  history_cursor?: string | null;
  event_cursor: number;
  status_slots: Record<string, string>;
  pending_inputs: PendingInput[];
  pending_interactions: PendingInteraction[];
}

/**
 * One unanswered client interaction, replayed by an open/resume response so a
 * reloaded client can present the dialog again.
 */
export interface PendingInteraction {
  type: string;
  data: JsonObject;
}

export interface PendingInput {
  message_id: string;
  content: string;
  target: "next-turn" | "next-step";
  media_count: number;
  artifact_count: number;
}

export type PermissionDecision = "allow" | "deny" | "ask";
export type SandboxAccess = "allow" | "deny" | "readonly" | "readwrite";

export interface SessionPolicy {
  session_id: string;
  permissions: Record<string, JsonObject[]>;
  effective_permissions: Record<string, JsonObject[]>;
  sandbox: JsonObject;
  effective_sandbox: JsonObject;
}

export interface SessionPolicyPatch {
  permissions?: Record<string, PermissionDecision>;
  remove_permissions?: string[];
  sandbox?: JsonObject;
  remove_sandbox?: string[];
}

export type PluginConfigScope = "global" | "workspace" | "session";

export interface PluginConfigDescriptor {
  plugin_id: string;
  name: string;
  editable: boolean;
  config_schema: JsonObject | null;
  scope_config: JsonObject;
  effective_config: JsonObject;
  unavailable_reason: string;
}

export interface PluginConfigCatalog {
  scope: PluginConfigScope;
  workspace_root: string;
  revision: string;
  applies_to: "new_sessions" | "current_session";
  plugins: PluginConfigDescriptor[];
}

export interface TrajectoryMessageItem {
  position: number;
  kind: "message";
  message_id: string;
  message: HistoryViewItem;
}

export interface TrajectorySurfaceReplaceItem {
  position: number;
  kind: "surface_replace";
  operation: string;
  transcript: "preserve" | "replace";
  source_node_ids: string[];
  messages: HistoryViewItem[];
  summary: string;
}

export interface TrajectoryEventItem {
  position: number;
  kind: "event";
  event: string;
  data: JsonObject;
  timestamp: string;
}

export type TrajectoryItem =
  | TrajectoryMessageItem
  | TrajectorySurfaceReplaceItem
  | TrajectoryEventItem;

export interface TrajectoryPage {
  items: TrajectoryItem[];
  next_cursor: string | null;
  // Absolute position of the newest record at the time of the request; the
  // client anchors its window on positions rather than on cursor offsets.
  newest_position: number;
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
  input_modalities: ("text" | "image" | "audio" | "video")[];
}

export interface AgentInfo {
  name: string;
  description: string;
  mode: "primary" | "subagent" | "all";
  model_policy: JsonObject;
  limits: JsonObject;
  tool_policy: JsonObject;
}

/** One entry of the agent-loop tool catalog (`GET .../tools`). */
export interface ToolInfo {
  name: string;
  registered_name: string;
  namespace: string;
  description: string;
  parameters: Record<string, unknown>;
  timeout_seconds: number | null;
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

export type CommandEffect = "history" | "thread" | "agents" | "jobs" | "commands" | "sessions";

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

export interface JobData {
  id: string;
  kind: string;
  label: string;
  state: "queued" | "running" | "succeeded" | "failed_before_start" | "failed_running" | "cancelled_before_start" | "cancelled_running";
  elapsed_ms: number;
  summary: string | null;
}

export interface TodoItemData {
  id: string;
  subject: string;
  description?: string;
  status: "pending" | "in_progress" | "completed";
  activeForm?: string;
  owner?: string;
  blocks: string[];
  blockedBy: string[];
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

export interface ViewEvent {
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
  context_tokens: 0,
  cache_read_input_tokens: 0,
  cache_creation_input_tokens: 0,
  prompt_cache_write_tokens: 0,
};

export const EMPTY_SESSION_STATS: SessionStatsData = {
  turns: 0,
  steps: 0,
  llm_ms: 0,
  tool_ms: 0,
  ttft_ms: 0,
  ttft_steps: 0,
  decode_ms: 0,
  decode_tokens: 0,
};
