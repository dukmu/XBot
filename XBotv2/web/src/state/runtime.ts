import {
  EMPTY_USAGE,
  type AgentInfo,
  type HistoryItem,
  type ImageReference,
  type InteractionRequest,
  type JsonObject,
  type OpenSessionResponse,
  type PendingInput,
  type ProviderInfo,
  type ServerEvent,
  type TaskData,
  type TodoItemData,
  type ThreadSummary,
  type ToolCall,
  type UsageData,
} from "../api/types";

export type TimelineEntry = MessageEntry | ToolEntry | NoticeEntry | RuntimeEntry;

export interface MessageEntry {
  id: string;
  kind: "message";
  role: "user" | "assistant";
  content: string;
  reasoning: string;
  streaming: boolean;
  images: MessageImage[];
}

export interface MessageImage {
  label: string;
  src?: string;
  href?: string;
}

export interface RuntimeEntry {
  id: string;
  kind: "runtime";
  source: string;
  event: string;
  content: string;
}

export interface ToolEntry {
  id: string;
  kind: "tool";
  toolCallId: string;
  name: string;
  args: unknown;
  status: string;
  result: unknown;
  data: unknown;
  error: JsonObject | null;
  artifacts: JsonObject[];
  images: JsonObject[];
}

export interface NoticeEntry {
  id: string;
  kind: "notice";
  level: "info" | "error";
  content: string;
}

export interface RuntimeState {
  serverReachable: boolean;
  sessionAttached: boolean;
  eventStreamConnected: boolean;
  catalogEventStreamConnected: boolean;
  loading: boolean;
  threads: ThreadSummary[];
  providers: ProviderInfo[];
  agents: AgentInfo[];
  current: RuntimeSession | null;
  entries: TimelineEntry[];
  assistantDraft: MessageEntry | null;
  historyCursor: string | null;
  historyLoading: boolean;
  tasks: Record<string, TaskData>;
  todos: TodoItemData[];
  interactions: InteractionRequest[];
  usage: UsageData;
  turnRunning: boolean;
  pendingInputs: PendingInput[];
  error: string;
}

export type RuntimeSession = Omit<
  OpenSessionResponse,
  "history" | "history_cursor" | "status" | "usage" | "pending_inputs"
>;

export type RuntimeAction =
  | { type: "loading"; value: boolean }
  | { type: "server_reachable"; value: boolean }
  | { type: "session_attached"; value: boolean }
  | { type: "event_stream"; value: boolean }
  | { type: "catalog_event_stream"; value: boolean }
  | { type: "threads"; threads: ThreadSummary[] }
  | { type: "providers"; providers: ProviderInfo[] }
  | { type: "agents"; agents: AgentInfo[] }
  | { type: "opened"; session: OpenSessionResponse }
  | { type: "session_deleted"; sessionId: string }
  | { type: "thread_synced"; thread: ThreadSummary }
  | { type: "history"; history: HistoryItem[]; nextCursor?: string | null }
  | { type: "history_prepend"; history: HistoryItem[]; nextCursor: string | null }
  | { type: "history_loading"; value: boolean }
  | { type: "tasks"; tasks: TaskData[] }
  | { type: "todos"; todos: TodoItemData[] }
  | { type: "pending_inputs"; items: PendingInput[] }
  | { type: "user_message"; id: string; content: string; images: MessageImage[] }
  | { type: "user_message_failed"; id: string }
  | { type: "event"; event: ServerEvent }
  | { type: "events"; events: ServerEvent[] }
  | { type: "turn_error"; message: string }
  | { type: "interaction_resolved"; requestId: string }
  | { type: "remove_task"; taskId: string }
  | { type: "agent_selected"; agent: string; provider: string; model: string; modelMode: string; contextWindow: number }
  | { type: "provider_selected"; provider: string; model: string; modelMode: string }
  | { type: "effort_selected"; modelMode: string }
  | { type: "error"; message: string }
  | { type: "clear_error" };

export const initialRuntimeState: RuntimeState = {
  serverReachable: false,
  sessionAttached: false,
  eventStreamConnected: false,
  catalogEventStreamConnected: false,
  loading: true,
  threads: [],
  providers: [],
  agents: [],
  current: null,
  entries: [],
  assistantDraft: null,
  historyCursor: null,
  historyLoading: false,
  tasks: {},
  todos: [],
  interactions: [],
  usage: { ...EMPTY_USAGE },
  turnRunning: false,
  pendingInputs: [],
  error: "",
};

export function runtimeReducer(state: RuntimeState, action: RuntimeAction): RuntimeState {
  switch (action.type) {
    case "loading":
      return { ...state, loading: action.value };
    case "server_reachable":
      return { ...state, serverReachable: action.value };
    case "session_attached":
      return { ...state, sessionAttached: action.value };
    case "event_stream":
      return { ...state, eventStreamConnected: action.value };
    case "catalog_event_stream":
      return { ...state, catalogEventStreamConnected: action.value };
    case "threads":
      return { ...state, threads: action.threads };
    case "providers":
      return { ...state, providers: action.providers };
    case "agents":
      return { ...state, agents: action.agents };
    case "opened":
      return {
        ...state,
        sessionAttached: true,
        loading: false,
        current: runtimeSession(action.session),
        entries: historyEntries(action.session.history),
        assistantDraft: null,
        historyCursor: action.session.history_cursor ?? null,
        historyLoading: false,
        usage: normalizeUsage(action.session.usage),
        interactions: [],
        tasks: {},
        todos: [],
        turnRunning: false,
        pendingInputs: action.session.pending_inputs || [],
        error: "",
      };
    case "session_deleted":
      if (state.current?.session_id !== action.sessionId) {
        return state;
      }
      return {
        ...state,
        loading: false,
        sessionAttached: false,
        eventStreamConnected: false,
        threads: [],
        agents: [],
        current: null,
        entries: [],
        assistantDraft: null,
        historyCursor: null,
        historyLoading: false,
        tasks: {},
        todos: [],
        interactions: [],
        usage: { ...EMPTY_USAGE },
        turnRunning: false,
        pendingInputs: [],
        error: "",
      };
    case "thread_synced":
      return state.current ? {
        ...state,
        current: {
          ...state.current,
          agent_name: action.thread.agent,
          provider: action.thread.provider,
          model: action.thread.model,
          model_mode: action.thread.model_mode,
          context_window: action.thread.context_window,
          status_slots: action.thread.status_slots,
          workspace_root: action.thread.workspace_root || state.current.workspace_root,
        },
        usage: normalizeUsage(action.thread.usage),
      } : state;
    case "history":
      return {
        ...state,
        entries: historyEntries(action.history),
        assistantDraft: null,
        historyCursor: action.nextCursor === undefined ? state.historyCursor : action.nextCursor,
      };
    case "history_prepend": {
      return {
        ...state,
        entries: [...historyEntries(action.history), ...state.entries],
        historyCursor: action.nextCursor,
        historyLoading: false,
      };
    }
    case "history_loading":
      return { ...state, historyLoading: action.value };
    case "tasks":
      return {
        ...state,
        tasks: Object.fromEntries(
          action.tasks
            .filter((task) => task.status !== "completed" && task.status !== "stopped")
            .map((task) => [task.task_id, task]),
        ),
      };
    case "todos":
      return { ...state, todos: action.todos };
    case "pending_inputs":
      return { ...state, pendingInputs: action.items };
    case "user_message":
      return {
        ...state,
        turnRunning: true,
        entries: [
          ...state.entries,
          { ...messageEntry("user", action.content), id: action.id, images: action.images },
        ],
      };
    case "user_message_failed":
      return {
        ...state,
        turnRunning: false,
        entries: state.entries.filter((entry) => entry.id !== action.id),
      };
    case "event":
      return applyEvent(state, action.event);
    case "events":
      return action.events.reduce(applyEvent, state);
    case "turn_error":
      return {
        ...state,
        turnRunning: false,
        entries: commitAssistantDraft(state.entries, state.assistantDraft),
        assistantDraft: null,
        error: action.message,
      };
    case "interaction_resolved":
      return {
        ...state,
        interactions: state.interactions.filter((item) => item.request_id !== action.requestId),
      };
    case "remove_task": {
      const tasks = { ...state.tasks };
      delete tasks[action.taskId];
      return { ...state, tasks };
    }
    case "agent_selected":
      return state.current ? {
        ...state,
        current: {
          ...state.current,
          agent_name: action.agent,
          provider: action.provider,
          model: action.model,
          model_mode: action.modelMode,
          context_window: action.contextWindow,
        },
      } : state;
    case "provider_selected":
      return state.current ? {
        ...state,
        current: {
          ...state.current,
          provider: action.provider,
          model: action.model,
          model_mode: action.modelMode,
        },
      } : state;
    case "effort_selected":
      return state.current ? {
        ...state,
        current: { ...state.current, model_mode: action.modelMode },
      } : state;
    case "error":
      return { ...state, error: action.message, loading: false };
    case "clear_error":
      return { ...state, error: "" };
  }
}

function applyEvent(state: RuntimeState, event: ServerEvent): RuntimeState {
  const data = event.data;
  switch (event.type) {
    case "turn_started":
      return { ...state, turnRunning: true };
    case "turn_finished":
    case "turn_cancelled":
      return {
        ...state,
        turnRunning: false,
        entries: commitAssistantDraft(state.entries, state.assistantDraft),
        assistantDraft: null,
        current: updateSlots(state.current, data.status_slots),
      };
    case "assistant_message_delta":
      return {
        ...state,
        assistantDraft: updateAssistantDraft(
          state.assistantDraft,
          stringValue(data.content),
          stringValue(data.reasoning),
        ),
      };
    case "assistant_message":
      return {
        ...state,
        entries: applyAssistantMessage(
          state.entries,
          state.assistantDraft,
          stringValue(data.content),
          arrayValue(data.tool_calls),
        ),
        assistantDraft: null,
      };
    case "tool_calls_started":
      return { ...state, entries: upsertToolCalls(state.entries, arrayValue(data.tool_calls)) };
    case "tool_call_delta":
      return { ...state, entries: applyToolDeltas(state.entries, arrayValue(data.tool_calls)) };
    case "tool_result":
      return { ...state, entries: applyToolResult(state.entries, data) };
    case "permission_request":
      return queueInteraction(state, permissionRequest(data));
    case "user_input_required":
      return queueInteraction(state, userInputRequest(data));
    case "permission_response_recorded":
    case "user_input_recorded":
      return {
        ...state,
        interactions: state.interactions.filter((item) => item.request_id !== stringValue(data.request_id)),
      };
    case "permission_denied":
      return {
        ...state,
        entries: [...state.entries, noticeEntry(stringValue(data.reason) || "Permission denied", "error")],
      };
    case "usage":
      return { ...state, usage: addUsage(state.usage, data) };
    case "queue_updated":
      return { ...state, pendingInputs: pendingInputs(data.items) };
    case "task_updated": {
      const task = data as unknown as TaskData;
      return { ...state, tasks: { ...state.tasks, [task.task_id]: task } };
    }
    case "todo_updated":
      return { ...state, todos: todoProjection(data) };
    case "client_message":
      return {
        ...state,
        entries: [...state.entries, noticeEntry(stringValue(data.message), "info")],
      };
    case "message": {
      const id = stringValue(data.id);
      if (!id || stringValue(data.role) !== "user") return state;
      const index = state.entries.findIndex((entry) => entry.id === id);
      if (index >= 0) return state;
      return {
        ...state,
        entries: [
          ...state.entries,
          {
            ...messageEntry("user", stringValue(data.content)),
            id,
            images: historyAttachments(
              arrayValue(data.images) as ImageReference[],
              arrayValue(data.artifacts).map(objectValue),
            ),
          },
        ],
      };
    }
    case "history_updated": {
      const history = arrayValue(data.history) as HistoryItem[];
      return {
        ...state,
        entries: historyEntries(history),
        assistantDraft: null,
        historyCursor: typeof data.history_cursor === "string" ? data.history_cursor : null,
      };
    }
    case "agent_configured":
      return state.current ? {
        ...state,
        current: {
          ...state.current,
          agent_name: stringValue(data.agent_name) || state.current.agent_name,
          provider: stringValue(data.provider) || state.current.provider,
          model: stringValue(data.model),
          model_mode: stringValue(data.model_mode),
          context_window: numberValue(data.context_window),
        },
      } : state;
    case "error":
      {
        const message = stringValue(data.message) || "XBot turn failed";
        return {
          ...state,
          turnRunning: false,
          entries: [
            ...commitAssistantDraft(state.entries, state.assistantDraft),
            noticeEntry(message, "error"),
          ],
          assistantDraft: null,
          error: message,
        };
      }
    default:
      return state;
  }
}

export function historyEntries(history: HistoryItem[]): TimelineEntry[] {
  let entries: TimelineEntry[] = [];
  for (const item of history) {
    if (item.role === "user") {
      if (item.runtime) {
        entries.push({
          id: nextId("runtime"),
          kind: "runtime",
          source: item.runtime.source ?? "runtime",
          event: item.runtime.event ?? "message",
          content: item.content,
        });
        continue;
      }
      entries.push({
        ...messageEntry("user", item.content),
        images: historyAttachments(item.images, item.artifacts),
      });
      continue;
    }
    if (item.role === "assistant") {
      if (item.content || item.reasoning) {
        entries.push({
          ...messageEntry("assistant", item.content),
          reasoning: item.reasoning || "",
        });
      }
      entries = upsertToolCalls(entries, item.tool_calls);
      continue;
    }
    entries = applyToolResult(entries, {
      tool_call_id: item.tool_call_id,
      content: item.content,
      status: item.status || "success",
      data: item.data,
      error: item.error,
      artifacts: item.artifacts,
      images: item.images,
    });
  }
  return entries;
}

function updateAssistantDraft(
  draft: MessageEntry | null,
  content: string,
  reasoning: string,
): MessageEntry {
  if (draft) {
    return {
      ...draft,
      content: draft.content + content,
      reasoning: draft.reasoning + reasoning,
    };
  }
  return { ...messageEntry("assistant", content), reasoning, streaming: true };
}

function applyAssistantMessage(
  entries: TimelineEntry[],
  draft: MessageEntry | null,
  content: string,
  calls: unknown[],
): TimelineEntry[] {
  let copy = entries;
  if (draft) {
    copy = [...copy, { ...draft, content: content || draft.content, streaming: false }];
  } else if (content) {
    copy = [...copy, messageEntry("assistant", content)];
  }
  copy = upsertToolCalls(copy, calls);
  return copy;
}

function commitAssistantDraft(
  entries: TimelineEntry[],
  draft: MessageEntry | null,
): TimelineEntry[] {
  return draft ? [...entries, { ...draft, streaming: false }] : entries;
}

function historyAttachments(images: ImageReference[] = [], artifacts: JsonObject[] = []): MessageImage[] {
  return [...images.map((image) => ({
    label: `${image.media_type} · ${formatBytes(image.size)}`,
    src: image.url,
    href: image.url,
  })), ...artifacts.map((artifact) => ({
    label: String(artifact.name || artifact.id || "attachment"),
    href: typeof artifact.url === "string" ? artifact.url : undefined,
  }))];
}

function formatBytes(size: number): string {
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} kB`;
  return `${(size / (1024 * 1024)).toFixed(1)} MB`;
}

function upsertToolCalls(entries: TimelineEntry[], rawCalls: unknown[]): TimelineEntry[] {
  const copy = [...entries];
  for (const raw of rawCalls) {
    const call = objectValue(raw);
    const id = stringValue(call.id) || stringValue(call.tool_call_id);
    if (!id) continue;
    const existing = copy.findIndex((entry) => entry.kind === "tool" && entry.toolCallId === id);
    const current = existing >= 0 ? copy[existing] as ToolEntry : null;
    const next: ToolEntry = {
      id: current?.id || nextId("tool"),
      kind: "tool",
      toolCallId: id,
      name: stringValue(call.name) || current?.name || "tool",
      args: call.args ?? current?.args ?? {},
      status: current?.status || "running",
      result: current?.result ?? null,
      data: current?.data ?? null,
      error: current?.error ?? null,
      artifacts: current?.artifacts ?? [],
      images: current?.images ?? [],
    };
    if (existing >= 0) copy[existing] = next;
    else copy.push(next);
  }
  return copy;
}

function applyToolDeltas(entries: TimelineEntry[], deltas: unknown[]): TimelineEntry[] {
  return upsertToolCalls(entries, deltas.map((raw) => {
    const item = objectValue(raw);
    return {
      id: stringValue(item.tool_call_id) || stringValue(item.id),
      name: item.name,
      args: item.args,
    };
  }));
}

function applyToolResult(entries: TimelineEntry[], data: JsonObject): TimelineEntry[] {
  const id = stringValue(data.tool_call_id);
  let copy = [...entries];
  let index = copy.findIndex((entry) => entry.kind === "tool" && entry.toolCallId === id);
  if (index < 0) {
    copy = upsertToolCalls(copy, [{ id, name: data.name || "tool", args: {} }]);
    index = copy.findIndex((entry) => entry.kind === "tool" && entry.toolCallId === id);
  }
  if (index >= 0) {
    const current = copy[index] as ToolEntry;
    copy[index] = {
      ...current,
      name: stringValue(data.name) || current.name,
      status: stringValue(data.status) || "success",
      result: data.content ?? "",
      data: data.data ?? null,
      error: data.error ? objectValue(data.error) : null,
      artifacts: arrayValue(data.artifacts).map(objectValue),
      images: arrayValue(data.images).map(objectValue),
    };
  }
  return copy;
}

function queueInteraction(state: RuntimeState, request: InteractionRequest): RuntimeState {
  if (!request.request_id || state.interactions.some((item) => item.request_id === request.request_id)) return state;
  return { ...state, interactions: [...state.interactions, request] };
}

function permissionRequest(data: JsonObject): InteractionRequest {
  const call = objectValue(data.tool_call) as unknown as ToolCall;
  return {
    kind: "permission",
    request_id: stringValue(data.request_id),
    source: stringValue(data.source),
    reason: stringValue(data.reason),
    tool_call: call,
    resume_supported: Boolean(data.resume_supported),
  };
}

function userInputRequest(data: JsonObject): InteractionRequest {
  return {
    kind: "user_input",
    request_id: stringValue(data.request_id),
    source: stringValue(data.source),
    tool_call_id: stringValue(data.tool_call_id),
    question: stringValue(data.question),
    options: arrayValue(data.options).map((option) => {
      const item = objectValue(option);
      return { label: stringValue(item.label), description: stringValue(item.description) };
    }),
    timeout_seconds: numberValue(data.timeout_seconds) || undefined,
    resume_supported: Boolean(data.resume_supported),
  };
}

function addUsage(current: UsageData, data: JsonObject): UsageData {
  return {
    input_tokens: current.input_tokens + numberValue(data.input_tokens),
    output_tokens: current.output_tokens + numberValue(data.output_tokens),
    total_tokens: current.total_tokens + numberValue(data.total_tokens),
    requests: current.requests + numberValue(data.requests),
    context_tokens: Object.hasOwn(data, "context_tokens")
      ? numberValue(data.context_tokens)
      : current.context_tokens,
    cache_read_input_tokens: current.cache_read_input_tokens + numberValue(data.cache_read_input_tokens),
    cache_creation_input_tokens: current.cache_creation_input_tokens + numberValue(data.cache_creation_input_tokens),
    prompt_cache_write_tokens: current.prompt_cache_write_tokens + numberValue(data.prompt_cache_write_tokens),
  };
}

function normalizeUsage(usage: Partial<UsageData>): UsageData {
  return {
    input_tokens: numberValue(usage.input_tokens),
    output_tokens: numberValue(usage.output_tokens),
    total_tokens: numberValue(usage.total_tokens),
    requests: numberValue(usage.requests),
    context_tokens: numberValue(usage.context_tokens),
    cache_read_input_tokens: numberValue(usage.cache_read_input_tokens),
    cache_creation_input_tokens: numberValue(usage.cache_creation_input_tokens),
    prompt_cache_write_tokens: numberValue(usage.prompt_cache_write_tokens),
  };
}

function updateSlots(current: RuntimeSession | null, slots: unknown): RuntimeSession | null {
  if (!current || !slots || typeof slots !== "object" || Array.isArray(slots)) return current;
  return { ...current, status_slots: slots as Record<string, string> };
}

function runtimeSession(session: OpenSessionResponse): RuntimeSession {
  return {
    session_id: session.session_id,
    thread_id: session.thread_id,
    agent_name: session.agent_name,
    workspace_root: session.workspace_root,
    provider: session.provider,
    model: session.model,
    model_mode: session.model_mode,
    context_window: session.context_window,
    event_cursor: session.event_cursor,
    status_slots: session.status_slots,
  };
}

function messageEntry(role: "user" | "assistant", content: string): MessageEntry {
  return { id: nextId(role), kind: "message", role, content, reasoning: "", streaming: false, images: [] };
}

function noticeEntry(content: string, level: "info" | "error"): NoticeEntry {
  return { id: nextId("notice"), kind: "notice", level, content };
}

let idSequence = 0;
function nextId(prefix: string): string {
  idSequence += 1;
  return `${prefix}-${idSequence}`;
}

function objectValue(value: unknown): JsonObject {
  return value && typeof value === "object" && !Array.isArray(value) ? value as JsonObject : {};
}

function arrayValue(value: unknown): unknown[] {
  return Array.isArray(value) ? value : [];
}

function stringValue(value: unknown): string {
  return typeof value === "string" ? value : "";
}

function numberValue(value: unknown): number {
  return typeof value === "number" && Number.isFinite(value) ? value : 0;
}

function pendingInputs(value: unknown): PendingInput[] {
  return arrayValue(value).flatMap((raw) => {
    const item = objectValue(raw);
    const messageId = stringValue(item.message_id);
    const target = stringValue(item.target);
    if (!messageId || (target !== "next-turn" && target !== "next-step")) return [];
    return [{
      message_id: messageId,
      content: stringValue(item.content),
      target,
      source: stringValue(item.source) || "user",
      image_count: numberValue(item.image_count),
      artifact_count: numberValue(item.artifact_count),
    }];
  });
}

function todoProjection(data: JsonObject): TodoItemData[] {
  if (data.kind !== "todo_snapshot") return [];
  return arrayValue(data.items).flatMap((value) => {
    const item = objectValue(value);
    const content = stringValue(item.content);
    const status = stringValue(item.status);
    return content && ["pending", "in_progress", "completed"].includes(status)
      ? [{ content, status: status as TodoItemData["status"] }]
      : [];
  });
}
