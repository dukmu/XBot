import {
  EMPTY_USAGE,
  type AgentInfo,
  type HistoryItem,
  type InteractionRequest,
  type JsonObject,
  type OpenSessionResponse,
  type ProviderInfo,
  type ServerEvent,
  type SessionSummary,
  type TaskData,
  type ThreadSummary,
  type ToolCall,
  type UsageData,
} from "../api/types";

export type TimelineEntry = MessageEntry | ToolEntry | NoticeEntry;

export interface MessageEntry {
  id: string;
  kind: "message";
  role: "user" | "assistant";
  content: string;
  reasoning: string;
  streaming: boolean;
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
}

export interface NoticeEntry {
  id: string;
  kind: "notice";
  level: "info" | "error";
  content: string;
}

export interface RuntimeState {
  connected: boolean;
  loading: boolean;
  sessions: SessionSummary[];
  threads: ThreadSummary[];
  providers: ProviderInfo[];
  agents: AgentInfo[];
  current: OpenSessionResponse | null;
  entries: TimelineEntry[];
  tasks: Record<string, TaskData>;
  interactions: InteractionRequest[];
  usage: UsageData;
  turnRunning: boolean;
  queuedMessages: number;
  error: string;
}

export type RuntimeAction =
  | { type: "loading"; value: boolean }
  | { type: "connected"; value: boolean }
  | { type: "sessions"; sessions: SessionSummary[] }
  | { type: "threads"; threads: ThreadSummary[] }
  | { type: "providers"; providers: ProviderInfo[] }
  | { type: "agents"; agents: AgentInfo[] }
  | { type: "opened"; session: OpenSessionResponse }
  | { type: "history"; history: HistoryItem[] }
  | { type: "tasks"; tasks: TaskData[] }
  | { type: "user_message"; content: string }
  | { type: "event"; event: ServerEvent }
  | { type: "interaction_resolved"; requestId: string }
  | { type: "remove_task"; taskId: string }
  | { type: "agent_selected"; agent: string; provider: string; model: string; modelMode: string; contextWindow: number }
  | { type: "provider_selected"; provider: string; model: string; modelMode: string }
  | { type: "error"; message: string }
  | { type: "clear_error" };

export const initialRuntimeState: RuntimeState = {
  connected: false,
  loading: true,
  sessions: [],
  threads: [],
  providers: [],
  agents: [],
  current: null,
  entries: [],
  tasks: {},
  interactions: [],
  usage: { ...EMPTY_USAGE },
  turnRunning: false,
  queuedMessages: 0,
  error: "",
};

export function runtimeReducer(state: RuntimeState, action: RuntimeAction): RuntimeState {
  switch (action.type) {
    case "loading":
      return { ...state, loading: action.value };
    case "connected":
      return { ...state, connected: action.value };
    case "sessions":
      return { ...state, sessions: action.sessions };
    case "threads":
      return { ...state, threads: action.threads };
    case "providers":
      return { ...state, providers: action.providers };
    case "agents":
      return { ...state, agents: action.agents };
    case "opened":
      return {
        ...state,
        connected: true,
        loading: false,
        current: action.session,
        entries: historyEntries(action.session.history),
        usage: { ...action.session.usage },
        interactions: [],
        tasks: {},
        turnRunning: false,
        queuedMessages: 0,
        error: "",
      };
    case "history":
      return { ...state, entries: historyEntries(action.history) };
    case "tasks":
      return {
        ...state,
        tasks: Object.fromEntries(
          action.tasks
            .filter((task) => task.status !== "completed" && task.status !== "stopped")
            .map((task) => [task.task_id, task]),
        ),
      };
    case "user_message":
      return {
        ...state,
        entries: [...state.entries, messageEntry("user", action.content)],
      };
    case "event":
      return applyEvent(state, action.event);
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
      return { ...state, turnRunning: true, queuedMessages: Math.max(0, state.queuedMessages - 1) };
    case "turn_finished":
    case "turn_cancelled":
      return {
        ...state,
        turnRunning: false,
        entries: finalizeAssistant(state.entries),
        current: updateSlots(state.current, data.status_slots),
      };
    case "assistant_message_delta":
      return {
        ...state,
        entries: updateAssistantDraft(
          state.entries,
          stringValue(data.content),
          stringValue(data.reasoning),
        ),
      };
    case "assistant_message":
      return {
        ...state,
        entries: applyAssistantMessage(
          state.entries,
          stringValue(data.content),
          arrayValue(data.tool_calls),
        ),
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
    case "message_queued":
      return {
        ...state,
        queuedMessages: Math.max(state.queuedMessages + 1, numberValue(data.position)),
      };
    case "task_updated": {
      const task = data as unknown as TaskData;
      return { ...state, tasks: { ...state.tasks, [task.task_id]: task } };
    }
    case "client_message":
      return {
        ...state,
        entries: [...state.entries, noticeEntry(stringValue(data.message), "info")],
      };
    case "error":
      return {
        ...state,
        error: stringValue(data.message) || "XBot turn failed",
        entries: [...state.entries, noticeEntry(stringValue(data.message) || "XBot turn failed", "error")],
      };
    default:
      return state;
  }
}

export function historyEntries(history: HistoryItem[]): TimelineEntry[] {
  let entries: TimelineEntry[] = [];
  for (const item of history) {
    if (item.role === "user") {
      entries.push(messageEntry("user", item.content));
      continue;
    }
    if (item.role === "assistant") {
      if (item.content) entries.push(messageEntry("assistant", item.content));
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
    });
  }
  return entries;
}

function updateAssistantDraft(entries: TimelineEntry[], content: string, reasoning: string): TimelineEntry[] {
  let copy = [...entries];
  const last = copy.at(-1);
  if (last?.kind === "message" && last.role === "assistant" && last.streaming) {
    copy[copy.length - 1] = {
      ...last,
      content: last.content + content,
      reasoning: last.reasoning + reasoning,
    };
    return copy;
  }
  copy.push({ ...messageEntry("assistant", content), reasoning, streaming: true });
  return copy;
}

function applyAssistantMessage(entries: TimelineEntry[], content: string, calls: unknown[]): TimelineEntry[] {
  let copy = [...entries];
  const last = copy.at(-1);
  if (last?.kind === "message" && last.role === "assistant" && last.streaming) {
    copy[copy.length - 1] = { ...last, content: content || last.content, streaming: false };
  } else if (content) {
    copy.push(messageEntry("assistant", content));
  }
  copy = upsertToolCalls(copy, calls);
  return copy;
}

function finalizeAssistant(entries: TimelineEntry[]): TimelineEntry[] {
  return entries.map((entry) => entry.kind === "message" && entry.streaming ? { ...entry, streaming: false } : entry);
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
    context_tokens: numberValue(data.context_tokens) || current.context_tokens,
  };
}

function updateSlots(current: OpenSessionResponse | null, slots: unknown): OpenSessionResponse | null {
  if (!current || !slots || typeof slots !== "object" || Array.isArray(slots)) return current;
  return { ...current, status_slots: slots as Record<string, string> };
}

function messageEntry(role: "user" | "assistant", content: string): MessageEntry {
  return { id: nextId(role), kind: "message", role, content, reasoning: "", streaming: false };
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
