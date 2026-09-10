import {
  EMPTY_USAGE,
  EMPTY_SESSION_STATS,
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
  type TrajectoryItem,
  type UsageData,
  type SessionStatsData,
} from "../api/types";

export type TimelineEntry = MessageEntry | ToolEntry | NoticeEntry | RuntimeEntry;
type TimelineOrigin = "trajectory" | "live";

export interface MessageEntry {
  id: string;
  origin?: TimelineOrigin;
  deliveryState?: "accepted" | "claimed" | "consumed";
  kind: "message";
  role: "user" | "assistant";
  content: string;
  reasoning: string;
  streaming: boolean;
  images: MessageImage[];
  messageId: string;
}

export interface MessageImage {
  label: string;
  src?: string;
  href?: string;
}

export interface RuntimeEntry {
  id: string;
  origin?: TimelineOrigin;
  kind: "runtime";
  source: string;
  event: string;
  content: string;
  messageId?: string;
}

export interface ToolEntry {
  id: string;
  origin?: TimelineOrigin;
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
  permissionRequestId?: string;
}

export interface NoticeEntry {
  id: string;
  origin?: TimelineOrigin;
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
  trajectory: TrajectoryItem[];
  trajectoryLoaded: boolean;
  tasks: Record<string, TaskData>;
  todos: TodoItemData[];
  interactions: InteractionRequest[];
  usage: UsageData;
  sessionStats: SessionStatsData;
  turnRunning: boolean;
  pendingInputs: PendingInput[];
  deliveryStates: Record<string, MessageEntry["deliveryState"]>;
  error: string;
}

export type RuntimeSession = Omit<
  OpenSessionResponse,
  "history" | "history_cursor" | "status" | "usage" | "session_stats" | "pending_inputs"
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
  | { type: "history_prepend"; history: HistoryItem[]; nextCursor: string | null; expectedCursor: string }
  | { type: "history_loading"; value: boolean }
  | { type: "trajectory"; items: TrajectoryItem[]; nextCursor: string | null; bufferedEvents?: ServerEvent[] }
  | { type: "trajectory_prepend"; items: TrajectoryItem[]; nextCursor: string | null; expectedCursor: string }
  | { type: "tasks"; tasks: TaskData[] }
  | { type: "todos"; todos: TodoItemData[] }
  | { type: "pending_inputs"; items: PendingInput[] }
  | { type: "pending_input_failed"; messageId: string }
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
  trajectory: [],
  trajectoryLoaded: false,
  tasks: {},
  todos: [],
  interactions: [],
  usage: { ...EMPTY_USAGE },
  sessionStats: { ...EMPTY_SESSION_STATS },
  turnRunning: false,
  pendingInputs: [],
  deliveryStates: {},
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
      return { ...state, threads: action.threads, ...currentThreadProjection(state, action.threads) };
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
        trajectory: [],
        trajectoryLoaded: false,
        usage: normalizeUsage(action.session.usage),
        sessionStats: normalizeSessionStats(action.session.session_stats),
        interactions: [],
        tasks: {},
        todos: [],
        turnRunning: false,
        pendingInputs: action.session.pending_inputs || [],
        deliveryStates: Object.fromEntries((action.session.pending_inputs || []).map((item) => [item.message_id, "accepted"])),
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
        trajectory: [],
        trajectoryLoaded: false,
        tasks: {},
        todos: [],
        interactions: [],
        usage: { ...EMPTY_USAGE },
        sessionStats: { ...EMPTY_SESSION_STATS },
        turnRunning: false,
        pendingInputs: [],
        deliveryStates: {},
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
        },
        usage: normalizeUsage(action.thread.usage),
        sessionStats: normalizeSessionStats(action.thread.session_stats),
        turnRunning: action.thread.turn_status === "running",
      } : state;
    case "history":
      return {
        ...state,
        entries: historyEntries(action.history),
        assistantDraft: null,
        historyCursor: action.nextCursor === undefined ? state.historyCursor : action.nextCursor,
      };
    case "history_prepend": {
      if (action.expectedCursor !== state.historyCursor) {
        return { ...state, historyLoading: false };
      }
      return {
        ...state,
        entries: [...historyEntries(action.history), ...state.entries],
        historyCursor: action.nextCursor,
        historyLoading: false,
      };
    }
    case "history_loading":
      return { ...state, historyLoading: action.value };
    case "trajectory":
      {
        // A trajectory refresh is authoritative for persisted records, but
        // frames received while it was in flight are not part of that page.
        // Keep those live entries until a later refresh folds them into the
        // durable baseline instead of briefly erasing them from the view.
        const live = state.trajectoryLoaded
          ? state.entries.filter((entry) => entry.origin !== "trajectory")
          : [];
      const baselineEntries = trajectoryEntries(action.items);
      const baseline = {
        ...state,
        trajectory: action.items,
        trajectoryLoaded: true,
        entries: mergeTrajectoryWithLive(baselineEntries, live),
        historyCursor: action.nextCursor,
      };
      return action.bufferedEvents?.length
        ? action.bufferedEvents.reduce(applyEvent, baseline)
        : baseline;
      }
    case "trajectory_prepend": {
      if (action.expectedCursor !== state.historyCursor) {
        return { ...state, historyLoading: false };
      }
      const trajectory = [...action.items, ...state.trajectory];
      const live = state.entries.filter((entry) => entry.origin !== "trajectory");
      return {
        ...state,
        trajectory,
        entries: mergeTrajectoryWithLive(trajectoryEntries(trajectory), live),
        historyCursor: action.nextCursor,
        historyLoading: false,
      };
    }
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
      return {
        ...state,
        pendingInputs: action.items,
        deliveryStates: {
          ...state.deliveryStates,
          ...Object.fromEntries(action.items.map((item) => [item.message_id, state.deliveryStates[item.message_id] || "accepted"])),
        },
      };
    case "pending_input_failed":
      return {
        ...state,
        pendingInputs: state.pendingInputs.filter((item) => item.message_id !== action.messageId),
        deliveryStates: Object.fromEntries(Object.entries(state.deliveryStates).filter(([id]) => id !== action.messageId)),
      };
    case "user_message":
      return {
        ...state,
        turnRunning: true,
        entries: [
          ...state.entries,
          {
            ...messageEntry("user", action.content),
            id: action.id,
            messageId: action.id,
            images: action.images,
            deliveryState: "accepted",
          },
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
      return {
        ...state,
        turnRunning: true,
        entries: [
          ...state.entries,
          runtimeEntry("turn", event.type, `Turn ${numberValue(data.turn) || "?"} started`, eventIdentity(event)),
        ],
        sessionStats: updateLiveTurn(state.sessionStats, data.turn),
        current: updateSlots(state.current, data.status_slots),
      };
    case "turn_finished":
    case "turn_cancelled":
      return {
        ...state,
        turnRunning: false,
        entries: [
          ...commitAssistantDraft(state.entries, state.assistantDraft),
          runtimeEntry(
            "turn",
            event.type,
            event.type === "turn_cancelled"
              ? `Turn ${numberValue(data.turn) || "?"} cancelled`
              : `Turn ${numberValue(data.turn) || "?"} finished`,
            eventIdentity(event),
          ),
        ],
        assistantDraft: null,
        current: updateSlots(state.current, data.status_slots),
        sessionStats: Object.hasOwn(data, "session_stats")
          ? normalizeSessionStats(data.session_stats)
          : state.sessionStats,
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
          stringValue(data.id),
        ),
        assistantDraft: null,
        sessionStats: addAssistantTiming(state.sessionStats, data.timing),
        current: updateSlots(state.current, data.status_slots),
      };
    case "tool_calls_started":
      return { ...state, entries: upsertToolCalls(state.entries, arrayValue(data.tool_calls)) };
    case "tool_call_delta":
      return { ...state, entries: applyToolDeltas(state.entries, arrayValue(data.tool_calls)) };
    case "tool_result":
      return {
        ...state,
        entries: applyToolResult(state.entries, data),
        sessionStats: addToolTiming(state.sessionStats, data.timing),
        current: updateSlots(state.current, data.status_slots),
      };
    case "permission_request":
      return queueInteraction({
        ...state,
        entries: upsertPermissionTool(state.entries, data),
      }, permissionRequest(data));
    case "user_input_required":
      return queueInteraction(state, userInputRequest(data));
    case "permission_response_recorded":
    case "user_input_recorded":
      return {
        ...state,
        entries: event.type === "permission_response_recorded"
          ? updatePermissionTool(state.entries, data, stringValue(data.decision) === "deny" ? "denied" : "approved")
          : state.entries,
        interactions: state.interactions.filter((item) => item.request_id !== stringValue(data.request_id)),
      };
    case "permission_denied":
    {
      const entries = updatePermissionTool(state.entries, data, "denied");
      return {
        ...state,
        entries: [...entries, noticeEntry(stringValue(data.reason) || "Permission denied", "error")],
      };
    }
    case "compaction_started":
      return { ...state, entries: [...state.entries, runtimeEntry("compact", event.type, "Compacting conversation history…", eventIdentity(event))] };
    case "compaction_completed":
      return { ...state, entries: [...state.entries, runtimeEntry("compact", event.type, "Conversation history compacted", eventIdentity(event))] };
    case "compaction_failed":
      return { ...state, entries: [...state.entries, runtimeEntry("compact", event.type, stringValue(data.error) || "Conversation compaction failed", eventIdentity(event))] };
    case "usage":
      return {
        ...state,
        usage: addUsage(state.usage, data),
        sessionStats: addUsageOutputTokens(state.sessionStats, data),
        current: updateSlots(state.current, data.status_slots),
      };
    case "queue_updated":
      return { ...state, pendingInputs: pendingInputs(data.items) };
    case "input_accepted":
    case "input_claimed":
    case "input_consumed": {
      const deliveryState = event.type === "input_accepted"
        ? "accepted"
        : event.type === "input_claimed" ? "claimed" : "consumed";
      const ids = new Set(arrayValue(data.message_ids).map(stringValue));
      return {
        ...state,
        deliveryStates: {
          ...state.deliveryStates,
          ...Object.fromEntries([...ids].map((id) => [id, deliveryState])),
        },
        pendingInputs: event.type === "input_consumed"
          ? state.pendingInputs.filter((item) => !ids.has(item.message_id))
          : state.pendingInputs,
        entries: state.entries.map((entry) => (
          entry.kind === "message" && ids.has(entry.messageId)
            ? { ...entry, deliveryState }
            : entry
        )),
      };
    }
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
        current: updateSlots(state.current, data.status_slots),
      };
    case "input_rejected":
      return {
        ...state,
        entries: [
          ...state.entries,
          noticeEntry(stringValue(data.reason) || "Input rejected", "error"),
        ],
        turnRunning: false,
      };
    case "message": {
      const id = stringValue(data.id);
      if (!id || stringValue(data.role) !== "user") return state;
      const runtime = objectValue(data.runtime);
      const runtimeSource = stringValue(runtime.source);
      const runtimeEvent = stringValue(runtime.event);
      const runtimeId = runtimeSource ? `runtime:${id}` : "";
      const index = state.entries.findIndex((entry) => (
        entry.id === id
        || ((entry.kind === "message" || entry.kind === "runtime") && entry.messageId === id)
        || (runtimeId !== "" && entry.id === runtimeId)
      ));
      if (index >= 0) {
        return runtimeSource
          ? state
          : {
            ...state,
            entries: state.entries.map((entry, entryIndex) => (
              entryIndex === index && entry.kind === "message"
                ? { ...entry, deliveryState: "consumed" }
                : entry
            )),
          };
      }
      return {
        ...state,
        pendingInputs: state.pendingInputs.filter((item) => item.message_id !== id),
        deliveryStates: { ...state.deliveryStates, ...(id ? { [id]: "consumed" } : {}) },
        entries: [
          ...state.entries,
          runtimeSource
            ? runtimeEntry(runtimeSource, runtimeEvent || "message", stringValue(data.content), runtimeId)
            : {
              ...messageEntry("user", stringValue(data.content)),
              id,
              messageId: id,
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
      const operation = stringValue(data.operation);
      const entries = historyEntries(history);
      if (operation.startsWith("compact:")) {
        entries.push(runtimeEntry(
          "compact",
          operation,
          "Conversation history compacted",
          eventIdentity(event),
        ));
      }
      if (state.trajectoryLoaded) {
        return {
          ...state,
          assistantDraft: null,
          historyCursor: null,
          sessionStats: Object.hasOwn(data, "session_stats")
            ? normalizeSessionStats(data.session_stats)
            : state.sessionStats,
        };
      }
      return {
        ...state,
        entries,
        assistantDraft: null,
        historyCursor: typeof data.history_cursor === "string" ? data.history_cursor : null,
        sessionStats: Object.hasOwn(data, "session_stats")
          ? normalizeSessionStats(data.session_stats)
          : state.sessionStats,
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
          messageId: item.id || "",
        });
        continue;
      }
      entries.push({
        ...messageEntry("user", item.content),
        messageId: item.id || "",
        images: historyAttachments(item.images, item.artifacts),
      });
      continue;
    }
    if (item.role === "assistant") {
      if (item.content || item.reasoning) {
        entries.push({
          ...messageEntry("assistant", item.content),
          messageId: item.id || "",
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

interface TrajectoryGroup {
  nodeId: string;
  entries: TimelineEntry[];
}

export function trajectoryEntries(items: TrajectoryItem[]): TimelineEntry[] {
  const groups: TrajectoryGroup[] = [];
  const lineage = new Map<string, string[]>();
  for (const item of items) {
    if (item.kind === "message") {
      const nodeId = String(item.position);
      groups.push({ nodeId, entries: positionedEntries(item.position, [item.message]) });
      lineage.set(nodeId, [nodeId]);
      continue;
    }
    if (item.kind === "event") {
      if (item.event.startsWith("compaction/")) {
        upsertCompactionGroup(groups, item.position, stringValue(item.data.compaction_id), item.event, item.data);
        continue;
      }
      groups.push({
        nodeId: `event:${item.position}`,
        entries: [runtimeEntry(
          "trajectory",
          item.event,
          trajectoryEventContent(item.event, item.data),
          `trajectory:${item.position}:event`,
        )],
      });
      continue;
    }

    const replacements = item.messages.map((message, index) => ({
      nodeId: `${item.position}:${index}`,
      entries: positionedEntries(item.position, [message], `replacement:${index}`),
    }));
    const sourceIds = item.source_node_ids.flatMap((source) => lineage.get(source) ?? [source]);
    if (item.transcript === "preserve") {
      if (replacements.length === 1) lineage.set(replacements[0].nodeId, sourceIds);
      const compactionId = item.operation.startsWith("compact:") ? item.operation.slice(8) : "";
      upsertCompactionGroup(groups, item.position, compactionId, item.operation, {});
      continue;
    }
    const indexes = sourceIds
      .map((source) => groups.findIndex((group) => group.nodeId === source))
      .filter((index) => index >= 0);
    const insertAt = indexes.length ? Math.min(...indexes) : groups.length;
    for (const index of [...indexes].sort((left, right) => right - left)) groups.splice(index, 1);
    groups.splice(insertAt, 0, ...replacements);
    for (const replacement of replacements) lineage.set(replacement.nodeId, [replacement.nodeId]);
  }
  return groups.flatMap((group) => group.entries).map((entry) => ({
    ...entry,
    origin: "trajectory" as const,
  }));
}

function upsertCompactionGroup(
  groups: TrajectoryGroup[],
  position: number,
  compactionId: string,
  event: string,
  data: JsonObject,
): void {
  const nodeId = `compact:${compactionId || position}`;
  const existing = groups.findIndex((group) => group.nodeId === nodeId);
  const previous = existing >= 0 ? groups[existing].entries[0] : undefined;
  const content = stringValue(data.error)
    || stringValue(data.summary)
    || (previous?.kind === "runtime" ? previous.content : "")
    || (event.endsWith("/start") ? "Compacting conversation history…" : "Conversation history compacted");
  const entry = runtimeEntry("compact", event, content, `trajectory:${position}:compact`);
  if (existing >= 0) groups[existing] = { nodeId, entries: [entry] };
  else groups.push({ nodeId, entries: [entry] });
}

function positionedEntries(position: number, history: HistoryItem[], suffix = "message"): TimelineEntry[] {
  return historyEntries(history).map((entry, index) => ({
    ...entry,
    id: `trajectory:${position}:${suffix}:${index}`,
  }));
}

function mergeTrajectoryWithLive(
  baseline: TimelineEntry[],
  live: TimelineEntry[],
): TimelineEntry[] {
  const baselineKeys = new Set(baseline.flatMap(entryIdentityKeys));
  return [
    ...baseline,
    ...live.filter((entry) => (
      entryIdentityKeys(entry).every((key) => !baselineKeys.has(key))
    )),
  ];
}

function entryIdentityKeys(entry: TimelineEntry): string[] {
  if (entry.kind === "message") {
    return entry.messageId ? [`message:${entry.messageId}`] : [];
  }
  if (entry.kind === "tool") {
    return entry.toolCallId ? [`tool:${entry.toolCallId}`] : [];
  }
  if (entry.kind === "runtime") {
    return entry.messageId ? [`runtime:${entry.messageId}`] : [];
  }
  return [];
}

function trajectoryEventContent(event: string, data: JsonObject): string {
  if (event === "compaction/summary") {
    return stringValue(data.summary) || "Conversation summary created";
  }
  if (event === "compaction/end") {
    return stringValue(data.error) || "Conversation compaction completed";
  }
  return stringValue(data.message) || event;
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
  messageId: string,
): TimelineEntry[] {
  if (messageId && entries.some((entry) => entry.kind === "message" && entry.messageId === messageId)) {
    return upsertToolCalls(entries, calls);
  }
  let copy = entries;
  if (draft) {
    copy = [...copy, { ...draft, content: content || draft.content, streaming: false, messageId }];
  } else if (content) {
    copy = [...copy, { ...messageEntry("assistant", content), messageId }];
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
      permissionRequestId: current?.permissionRequestId,
    };
    if (existing >= 0) copy[existing] = next;
    else copy.push(next);
  }
  return copy;
}

function applyToolDeltas(entries: TimelineEntry[], deltas: unknown[]): TimelineEntry[] {
  let copy = [...entries];
  for (const raw of deltas) {
    const item = objectValue(raw);
    const id = stringValue(item.tool_call_id) || stringValue(item.id);
    const previousId = stringValue(item.replaces_tool_call_id);
    if (previousId && previousId !== id && previousId.startsWith("tool_")) {
      copy = renameToolCall(copy, previousId, id);
    }
    copy = upsertToolCalls(copy, [{
      id,
      name: item.name,
      args: item.args,
    }]);
  }
  return copy;
}

function renameToolCall(entries: TimelineEntry[], previousId: string, id: string): TimelineEntry[] {
  const previousIndex = entries.findIndex(
    (entry) => entry.kind === "tool" && entry.toolCallId === previousId,
  );
  if (previousIndex < 0 || !id) return entries;
  const currentIndex = entries.findIndex(
    (entry) => entry.kind === "tool" && entry.toolCallId === id,
  );
  const copy = [...entries];
  if (currentIndex >= 0) {
    copy.splice(previousIndex, 1);
    return copy;
  }
  const previous = copy[previousIndex] as ToolEntry;
  copy[previousIndex] = { ...previous, toolCallId: id };
  return copy;
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

function upsertPermissionTool(entries: TimelineEntry[], data: JsonObject): TimelineEntry[] {
  const call = objectValue(data.tool_call);
  const id = stringValue(call.id) || stringValue(call.tool_call_id);
  if (!id) return entries;
  const next = upsertToolCalls(entries, [{ id, name: call.name, args: call.args }]);
  return next.map((entry) => entry.kind === "tool" && entry.toolCallId === id
    ? {
      ...entry,
      status: entry.status === "success" || entry.status === "error" ? entry.status : "pending",
      permissionRequestId: stringValue(data.request_id),
    }
    : entry);
}

function updatePermissionTool(entries: TimelineEntry[], data: JsonObject, status: string): TimelineEntry[] {
  const requestId = stringValue(data.request_id);
  if (!requestId) return entries;
  return entries.map((entry) => entry.kind === "tool" && entry.permissionRequestId === requestId
    ? { ...entry, status }
    : entry);
}

function runtimeEntry(source: string, event: string, content: string, id?: string): RuntimeEntry {
  return { id: id || nextId("runtime"), kind: "runtime", source, event, content, messageId: "" };
}

function eventIdentity(event: ServerEvent): string {
  return `event:${event.sequence}:${event.type}`;
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

function updateLiveTurn(current: SessionStatsData, turn: unknown): SessionStatsData {
  const value = numberValue(turn);
  return value > current.turns ? { ...current, turns: value } : current;
}

function addAssistantTiming(current: SessionStatsData, value: unknown): SessionStatsData {
  const timing = objectValue(value);
  const llm = numberValue(timing.llm_ms);
  const ttft = numberValue(timing.ttft_ms);
  const decode = numberValue(timing.decode_ms);
  if (!(llm || ttft || decode)) return current;
  return {
    ...current,
    steps: current.steps + 1,
    llm_ms: current.llm_ms + llm,
    ttft_ms: current.ttft_ms + ttft,
    ttft_steps: current.ttft_steps + (Object.hasOwn(timing, "ttft_ms") ? 1 : 0),
    decode_ms: current.decode_ms + decode,
  };
}

function addToolTiming(current: SessionStatsData, value: unknown): SessionStatsData {
  const duration = numberValue(objectValue(value).duration_ms);
  return duration ? { ...current, tool_ms: current.tool_ms + duration } : current;
}

function addUsageOutputTokens(current: SessionStatsData, data: JsonObject): SessionStatsData {
  const output = numberValue(data.output_tokens);
  return output ? { ...current, decode_tokens: current.decode_tokens + output } : current;
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

function normalizeSessionStats(value: unknown): SessionStatsData {
  const stats = objectValue(value);
  return {
    turns: numberValue(stats.turns),
    steps: numberValue(stats.steps),
    llm_ms: numberValue(stats.llm_ms),
    tool_ms: numberValue(stats.tool_ms),
    ttft_ms: numberValue(stats.ttft_ms),
    ttft_steps: numberValue(stats.ttft_steps),
    decode_ms: numberValue(stats.decode_ms),
    decode_tokens: numberValue(stats.decode_tokens),
  };
}

function updateSlots(current: RuntimeSession | null, slots: unknown): RuntimeSession | null {
  if (!current || !slots || typeof slots !== "object" || Array.isArray(slots)) return current;
  return { ...current, status_slots: slots as Record<string, string> };
}

function currentThreadProjection(
  state: RuntimeState,
  threads: ThreadSummary[],
): Pick<RuntimeState, "current" | "usage" | "sessionStats"> {
  const current = state.current;
  if (!current) return {
    current: null,
    usage: state.usage,
    sessionStats: state.sessionStats,
  };
  const thread = threads.find((item) => item.thread_id === current.thread_id);
  if (!thread) return {
    current,
    usage: state.usage,
    sessionStats: state.sessionStats,
  };
  return {
    current: {
      ...current,
      agent_name: thread.agent,
      provider: thread.provider,
      model: thread.model,
      model_mode: thread.model_mode,
      context_window: thread.context_window,
      status_slots: thread.status_slots,
    },
    usage: mergeLiveUsage(state.usage, thread.usage),
    sessionStats: mergeLiveStats(state.sessionStats, thread.session_stats),
  };
}

function mergeLiveUsage(current: UsageData, snapshot: UsageData): UsageData {
  const next = normalizeUsage(snapshot);
  return {
    input_tokens: Math.max(current.input_tokens, next.input_tokens),
    output_tokens: Math.max(current.output_tokens, next.output_tokens),
    total_tokens: Math.max(current.total_tokens, next.total_tokens),
    requests: Math.max(current.requests, next.requests),
    // Context is a point-in-time value. Do not let a concurrently returned
    // snapshot move a newer stream event backwards during a running turn.
    context_tokens: Math.max(current.context_tokens, next.context_tokens),
    cache_read_input_tokens: Math.max(current.cache_read_input_tokens, next.cache_read_input_tokens),
    cache_creation_input_tokens: Math.max(current.cache_creation_input_tokens, next.cache_creation_input_tokens),
    prompt_cache_write_tokens: Math.max(current.prompt_cache_write_tokens, next.prompt_cache_write_tokens),
  };
}

function mergeLiveStats(current: SessionStatsData, snapshot: SessionStatsData): SessionStatsData {
  const next = normalizeSessionStats(snapshot);
  return {
    turns: Math.max(current.turns, next.turns),
    steps: Math.max(current.steps, next.steps),
    llm_ms: Math.max(current.llm_ms, next.llm_ms),
    tool_ms: Math.max(current.tool_ms, next.tool_ms),
    ttft_ms: Math.max(current.ttft_ms, next.ttft_ms),
    ttft_steps: Math.max(current.ttft_steps, next.ttft_steps),
    decode_ms: Math.max(current.decode_ms, next.decode_ms),
    decode_tokens: Math.max(current.decode_tokens, next.decode_tokens),
  };
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
  return { id: nextId(role), kind: "message", role, content, reasoning: "", streaming: false, images: [], messageId: "" };
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
