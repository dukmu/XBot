import { useCallback, useEffect, useMemo, useReducer, useRef, useState, useSyncExternalStore } from "react";
import { XBotApi, XBotApiError } from "../api/client";
import type { CommandInfo, CommandResultData, InteractionRequest, OpenSessionResponse, TaskData, ThreadSummary } from "../api/types";
import type { PendingAttachment } from "../components/Composer";
import { WorkspaceManager } from "../client/WorkspaceManager";
import { SessionCatalog } from "../client/SessionCatalog";
import { RuntimeEventController } from "../client/RuntimeEventController";
import { WorkspaceCatalogController } from "../client/WorkspaceCatalogController";
import { initialRuntimeState, runtimeReducer } from "./runtime";

const apiBase = import.meta.env.VITE_XBOT_API_BASE || "/api";

export function useXBot() {
  const api = useMemo(() => new XBotApi(apiBase), []);
  const [state, dispatch] = useReducer(runtimeReducer, initialRuntimeState);
  const [commands, setCommands] = useState<CommandInfo[]>([]);
  const [commandRunning, setCommandRunning] = useState(false);
  const [notification, setNotification] = useState("");
  const workspaceManager = useMemo(() => new WorkspaceManager(api), [api]);
  const sessionCatalog = useMemo(() => new SessionCatalog(api), [api]);
  const workspaces = useSyncExternalStore(workspaceManager.subscribe, workspaceManager.getSnapshot);
  const sessions = useSyncExternalStore(sessionCatalog.subscribe, sessionCatalog.getSnapshot);
  const messageControllers = useRef(new Map<AbortController, string>());
  const navigationGeneration = useRef(0);
  const commandInFlight = useRef(false);
  const sessionMutationInFlight = useRef(false);
  const navigationBlocked = state.loading || commandRunning;
  const navigationBlockMessage = state.loading
    ? "Wait for the active session operation before switching sessions."
    : commandRunning
      ? "Wait for the active command before switching sessions."
      : "Wait for the active command before switching sessions.";
  const notify = useCallback((message: string) => setNotification(message), []);

  const reportError = useCallback((error: unknown, turnFailed = false) => {
    if (error instanceof DOMException && error.name === "AbortError") return;
    const message = error instanceof XBotApiError
      ? `${error.code}: ${error.message}`
      : error instanceof Error ? error.message : String(error);
    dispatch({ type: turnFailed ? "turn_error" : "error", message });
  }, []);

  const runtimeEvents = useMemo(() => new RuntimeEventController(api, {
    onEvents: (events) => dispatch({ type: "events", events }),
    onThreads: (threads) => dispatch({ type: "threads", threads }),
    onTaskExpired: (taskId) => dispatch({ type: "remove_task", taskId }),
    onConnection: (connected) => dispatch({ type: "event_stream", value: connected }),
    onError: reportError,
  }), [api, reportError]);
  const workspaceCatalog = useMemo(() => new WorkspaceCatalogController(
    api,
    sessionCatalog,
    workspaceManager,
    {
      onConnection: (connected) => dispatch({ type: "catalog_event_stream", value: connected }),
      onError: reportError,
    },
  ), [api, reportError, sessionCatalog, workspaceManager]);

  const resetStreamingState = useCallback(() => runtimeEvents.stop(), [runtimeEvents]);

  const startEventStream = useCallback((session: Pick<OpenSessionResponse, "session_id" | "thread_id" | "event_cursor">, generation: number) => {
    runtimeEvents.start(session, generation);
  }, [runtimeEvents]);

  const activate = useCallback(async (session: OpenSessionResponse, generation: number) => {
    if (generation !== navigationGeneration.current) return;
    let resources: [ThreadSummary[], Awaited<ReturnType<XBotApi["listAgents"]>>, TaskData[], CommandInfo[], Awaited<ReturnType<XBotApi["listTodos"]>>];
    try {
      resources = await Promise.all([
        api.listThreads(session.session_id),
        api.listAgents(session.session_id, session.thread_id),
        api.listTasks(session.session_id, session.thread_id),
        api.listCommands(session.session_id, session.thread_id),
        api.listTodos(session.session_id, session.thread_id).catch((error) => {
          if (error instanceof XBotApiError && error.code === "capability_unavailable") return [];
          throw error;
        }),
      ]);
    } catch (error) {
      throw error;
    }
    const [threads, agents, tasks, availableCommands, todos] = resources;
    if (generation !== navigationGeneration.current) return;
    resetStreamingState();
    dispatch({ type: "opened", session });
    dispatch({ type: "threads", threads });
    const activeThread = threads.find((thread) => thread.thread_id === session.thread_id);
    if (activeThread) dispatch({ type: "thread_synced", thread: activeThread });
    dispatch({ type: "agents", agents });
    dispatch({ type: "tasks", tasks });
    dispatch({ type: "todos", todos });
    setCommands(availableCommands);
    setNotification("");
    startEventStream(session, generation);
  }, [api, resetStreamingState, startEventStream]);

  const refreshSessions = useCallback(async () => {
    try {
      await workspaceCatalog.refresh();
    } catch (error) {
      reportError(error);
    }
  }, [workspaceCatalog, reportError]);

  useEffect(() => {
    let alive = true;
    void (async () => {
      try {
        await api.hello();
        const [, providers] = await Promise.all([
          workspaceCatalog.start(),
          api.listProviders(),
        ]);
        if (!alive) return;
        dispatch({ type: "providers", providers: providers.providers });
        dispatch({ type: "server_reachable", value: true });
        dispatch({ type: "loading", value: false });
      } catch (error) {
        if (alive) reportError(error);
      }
    })();
    return () => {
      alive = false;
      workspaceCatalog.stop();
      runtimeEvents.stop();
      for (const controller of messageControllers.current.keys()) controller.abort();
    };
  }, [api, workspaceCatalog, reportError, runtimeEvents]);

  const openExistingSession = useCallback(async (sessionId?: string, workspaceRoot?: string) => {
    if (navigationBlocked || commandInFlight.current) {
      notify(navigationBlockMessage);
      return;
    }
    const id = sessionId || state.current?.session_id;
    if (!id) return;
    const previous = state.current;
    const generation = ++navigationGeneration.current;
    dispatch({ type: "loading", value: true });
    try {
      const threads = await api.listThreads(id);
      if (generation !== navigationGeneration.current) return;
      const mainThread = threads.find((thread) => thread.kind === "main") || threads[0];
      if (!mainThread) throw new Error(`Session ${id} has no resumable threads`);
      const session = await api.openSession({
        sessionId: id,
        threadId: mainThread.thread_id,
        workspaceRoot: workspaceRoot?.trim() || mainThread.workspace_root,
        mode: "resume",
      });
      if (generation !== navigationGeneration.current) return;
      await activate(session, generation);
      if (generation === navigationGeneration.current) await refreshSessions();
    } catch (error) {
      if (generation === navigationGeneration.current) {
        if (previous) startEventStream(previous, generation);
        reportError(error);
      }
    } finally {
      if (generation === navigationGeneration.current) dispatch({ type: "loading", value: false });
    }
  }, [activate, api, navigationBlockMessage, navigationBlocked, notify, refreshSessions, reportError, startEventStream, state.current]);

  const createSession = useCallback(async (workspaceRoot: string) => {
    const requestedWorkspace = workspaceRoot.trim();
    const targetWorkspace = requestedWorkspace
      || state.current?.workspace_root
      || workspaces.items[0]?.path
      || "";
    const reusable = workspaceCatalog.reusableSession(targetWorkspace);
    if (reusable) {
      await openExistingSession(reusable.session_id, targetWorkspace);
      return;
    }
    if (navigationBlocked) {
      notify(navigationBlockMessage);
      return;
    }
    const previous = state.current;
    const generation = ++navigationGeneration.current;
    dispatch({ type: "loading", value: true });
    try {
      if (targetWorkspace) await workspaceManager.create(targetWorkspace);
      if (generation !== navigationGeneration.current) return;
      const session = await api.openSession({
        workspaceRoot: targetWorkspace || undefined,
        mode: "new",
      });
      if (generation !== navigationGeneration.current) return;
      await activate(session, generation);
      if (generation === navigationGeneration.current) await refreshSessions();
    } catch (error) {
      if (generation === navigationGeneration.current) {
        if (previous) startEventStream(previous, generation);
        reportError(error);
      }
    } finally {
      if (generation === navigationGeneration.current) dispatch({ type: "loading", value: false });
    }
  }, [activate, api, workspaceCatalog, navigationBlockMessage, navigationBlocked, notify, openExistingSession, refreshSessions, reportError, startEventStream, state.current, workspaceManager, workspaces]);

  const renameWorkspace = useCallback(async (workspaceId: string, title: string) => {
    try {
      await workspaceManager.rename(workspaceId, title);
    } catch (error) {
      reportError(error);
    }
  }, [reportError, workspaceManager]);

  const deleteWorkspace = useCallback(async (workspaceId: string) => {
    try {
      await workspaceManager.delete(workspaceId);
    } catch (error) {
      reportError(error);
    }
  }, [reportError, workspaceManager]);

  const moveWorkspace = useCallback(async (workspaceId: string, direction: -1 | 1) => {
    try {
      await workspaceManager.move(workspaceId, direction);
    } catch (error) {
      reportError(error);
    }
  }, [reportError, workspaceManager]);

  const setSessionArchived = useCallback(async (sessionId: string, archived: boolean) => {
    try {
      await workspaceManager.setSessionArchived(sessionId, archived);
    } catch (error) {
      reportError(error);
    }
  }, [reportError, workspaceManager]);

  const moveSession = useCallback(async (
    workspaceId: string,
    sessionId: string,
    direction: -1 | 1,
  ) => {
    try {
      await workspaceManager.moveSession(workspaceId, sessionId, direction);
    } catch (error) {
      reportError(error);
    }
  }, [reportError, workspaceManager]);

  const resumeSession = openExistingSession;

  const listDirectories = useCallback((path?: string, signal?: AbortSignal) => (
    api.listDirectories(path, signal)
  ), [api]);

  const selectThread = useCallback(async (thread: ThreadSummary) => {
    if (!state.current || thread.thread_id === state.current.thread_id) return;
    if (navigationBlocked) {
      notify(state.loading
        ? "Wait for the active session operation before switching threads."
        : commandRunning
          ? "Wait for the active command before switching threads."
          : "Finish or interrupt the active turn before switching threads.");
      return;
    }
    const previous = state.current;
    const generation = ++navigationGeneration.current;
    dispatch({ type: "loading", value: true });
    try {
      const session = thread.kind === "main"
        ? await api.openSession({
          sessionId: state.current.session_id,
          threadId: thread.thread_id,
          workspaceRoot: thread.workspace_root,
          mode: "resume",
        })
        : await openSubagentThread(api, state.current.thread_id, state.current.session_id, thread);
      if (generation !== navigationGeneration.current) return;
      await activate(session, generation);
    } catch (error) {
      if (generation === navigationGeneration.current) {
        startEventStream(previous, generation);
        reportError(error);
      }
    } finally {
      if (generation === navigationGeneration.current) dispatch({ type: "loading", value: false });
    }
  }, [activate, api, commandRunning, navigationBlocked, notify, reportError, startEventStream, state.current, state.loading]);

  const sendMessage = useCallback(async (
    rawContent: string,
    attachments: PendingAttachment[] = [],
  ): Promise<boolean> => {
    const current = state.current;
    const generation = navigationGeneration.current;
    const content = rawContent.trim();
    if (!current || state.loading || (!content && attachments.length === 0)) return false;
    const requestId = crypto.randomUUID();
    const requestTarget = `${current.session_id}\n${current.thread_id}`;
    const delivery = (
      state.turnRunning || [...messageControllers.current.values()].includes(requestTarget)
    ) ? "queue" : "steer";
    if (delivery === "steer") {
      dispatch({
        type: "user_message",
        id: requestId,
        content,
        images: attachments.map((attachment) => ({ label: attachment.name, src: attachment.preview })),
      });
    }
    const controller = new AbortController();
    messageControllers.current.set(controller, requestTarget);
    try {
      for await (const _event of api.sendMessage(
        current.session_id,
        current.thread_id,
        content,
        attachments
          .filter((attachment) => attachment.media_type.startsWith("image/"))
          .map(({ data, media_type }) => ({ data, media_type })),
        attachments
          .filter((attachment) => !attachment.media_type.startsWith("image/"))
          .map(({ data, media_type, name }) => ({ data, media_type, name })),
        controller.signal,
        requestId,
        delivery,
      )) {
        // The session event connection is the authoritative, resumable
        // delivery path. The POST stream is drained only for compatibility
        // with older clients and to surface transport-level failures.
      }
      return true;
    } catch (error) {
      if (generation === navigationGeneration.current && delivery === "steer") {
        dispatch({ type: "user_message_failed", id: requestId });
      }
      if (generation === navigationGeneration.current) reportError(error, delivery === "steer");
      return false;
    } finally {
      messageControllers.current.delete(controller);
    }
  }, [api, reportError, state.current, state.loading, state.turnRunning]);

  const updatePendingInput = useCallback(async (
    messageId: string,
    action: { action: "edit"; content: string } | { action: "remove" | "steer" },
  ): Promise<boolean> => {
    const current = state.current;
    if (!current) return false;
    try {
      const result = await api.updatePendingInput(
        current.session_id,
        current.thread_id,
        messageId,
        action,
      );
      dispatch({ type: "pending_inputs", items: result.items });
      return true;
    } catch (error) {
      reportError(error);
      return false;
    }
  }, [api, reportError, state.current]);

  const retryLast = useCallback(async () => {
    if (state.turnRunning || !state.current) return;
    const current = state.current;
    const generation = navigationGeneration.current;
    const controller = new AbortController();
    messageControllers.current.set(controller, `${current.session_id}\n${current.thread_id}`);
    try {
      for await (const _event of api.regenerateMessage(
        current.session_id,
        current.thread_id,
        controller.signal,
      )) {
        // Runtime events arrive through the resumable session stream.
      }
    } catch (error) {
      if (generation === navigationGeneration.current) reportError(error, true);
    } finally {
      messageControllers.current.delete(controller);
    }
  }, [api, reportError, runtimeEvents, state.current, state.turnRunning]);

  const loadEarlier = useCallback(async () => {
    const current = state.current;
    const cursor = state.historyCursor;
    if (!current || !cursor || state.historyLoading) return;
    const generation = navigationGeneration.current;
    dispatch({ type: "history_loading", value: true });
    try {
      const page = await api.listMessages(current.session_id, current.thread_id, {
        cursor,
        limit: 80,
      });
      if (generation !== navigationGeneration.current) return;
      dispatch({
        type: "history_prepend",
        history: page.messages,
        nextCursor: page.next_cursor,
      });
    } catch (error) {
      if (generation === navigationGeneration.current) reportError(error);
    } finally {
      if (generation === navigationGeneration.current) {
        dispatch({ type: "history_loading", value: false });
      }
    }
  }, [api, reportError, state.current, state.historyCursor, state.historyLoading]);

  const interrupt = useCallback(async () => {
    if (!state.current) return;
    try {
      await api.interrupt(state.current.session_id, state.current.thread_id);
    } catch (error) {
      reportError(error);
    }
  }, [api, reportError, state.current]);

  const resolveInteraction = useCallback(async (
    request: InteractionRequest,
    answer: unknown,
    scope: "once" | "session" = "once",
  ) => {
    if (!state.current) return;
    try {
      if (request.kind === "permission") {
        await api.respondPermission(
          state.current.session_id,
          state.current.thread_id,
          request.request_id,
          answer as "allow" | "deny",
          scope,
        );
      } else {
        await api.respondUserInput(
          state.current.session_id,
          state.current.thread_id,
          request.request_id,
          answer,
        );
      }
      dispatch({ type: "interaction_resolved", requestId: request.request_id });
    } catch (error) {
      reportError(error);
    }
  }, [api, reportError, state.current]);

  const selectAgent = useCallback(async (name: string) => {
    if (!state.current || state.turnRunning) return;
    const generation = navigationGeneration.current;
    try {
      const result = await api.selectAgent(state.current.session_id, state.current.thread_id, name);
      if (generation !== navigationGeneration.current) return;
      dispatch({
        type: "agent_selected",
        agent: result.agent,
        provider: result.provider,
        model: result.model,
        modelMode: result.model_mode,
        contextWindow: result.context_window,
      });
    } catch (error) {
      if (generation === navigationGeneration.current) reportError(error);
    }
  }, [api, reportError, state.current, state.turnRunning]);

  const selectProvider = useCallback(async (name: string, model?: string) => {
    if (!state.current || state.turnRunning) return;
    const generation = navigationGeneration.current;
    try {
      const result = await api.selectProvider(
        state.current.session_id,
        state.current.thread_id,
        name,
        model,
      );
      if (generation !== navigationGeneration.current) return;
      dispatch({
        type: "provider_selected",
        provider: result.provider,
        model: result.model,
        modelMode: result.model_mode,
      });
    } catch (error) {
      if (generation === navigationGeneration.current) reportError(error);
    }
  }, [api, reportError, state.current, state.turnRunning]);

  const selectEffort = useCallback(async (effort: string) => {
    if (!state.current || state.turnRunning) return;
    const generation = navigationGeneration.current;
    try {
      const result = await api.selectEffort(
        state.current.session_id,
        state.current.thread_id,
        effort,
      );
      if (generation !== navigationGeneration.current) return;
      dispatch({ type: "effort_selected", modelMode: result.model_mode });
    } catch (error) {
      if (generation === navigationGeneration.current) reportError(error);
    }
  }, [api, reportError, state.current, state.turnRunning]);

  const undo = useCallback(async (count = 1) => {
    if (!state.current) return;
    if (state.turnRunning) {
      notify("Finish or interrupt the active turn before changing history.");
      return;
    }
    const current = state.current;
    const generation = navigationGeneration.current;
    try {
      const result = await api.undoHistory(current.session_id, current.thread_id, count);
      if (generation !== navigationGeneration.current) return;
      dispatch({
        type: "history",
        history: result.messages,
        nextCursor: result.history_cursor ?? null,
      });
      const threads = await api.listThreads(current.session_id);
      if (generation !== navigationGeneration.current) return;
      dispatch({ type: "threads", threads });
      const thread = threads.find((item) => item.thread_id === current.thread_id);
      if (thread) dispatch({ type: "thread_synced", thread });
      await refreshSessions();
    } catch (error) {
      if (generation === navigationGeneration.current) reportError(error);
    }
  }, [api, notify, refreshSessions, reportError, state.current, state.turnRunning]);

  const clear = useCallback(async () => {
    if (!state.current) return;
    if (state.turnRunning) {
      notify("Finish or interrupt the active turn before changing history.");
      return;
    }
    const current = state.current;
    const generation = navigationGeneration.current;
    try {
      const result = await api.clearHistory(current.session_id, current.thread_id);
      if (generation !== navigationGeneration.current) return;
      dispatch({ type: "history", history: result.messages, nextCursor: null });
      const threads = await api.listThreads(current.session_id);
      if (generation !== navigationGeneration.current) return;
      dispatch({ type: "threads", threads });
      const thread = threads.find((item) => item.thread_id === current.thread_id);
      if (thread) dispatch({ type: "thread_synced", thread });
      await refreshSessions();
    } catch (error) {
      if (generation === navigationGeneration.current) reportError(error);
    }
  }, [api, notify, refreshSessions, reportError, state.current, state.turnRunning]);

  const forkSession = useCallback(async (sessionId: string) => {
    if (navigationBlocked) {
      notify(state.loading
        ? "Wait for the active session operation before forking a session."
        : commandRunning
          ? "Wait for the active command before forking a session."
          : "Finish or interrupt the active turn before forking a session.");
      return;
    }
    if (sessionMutationInFlight.current) {
      notify("Another session operation is still running.");
      return;
    }
    const generation = navigationGeneration.current;
    sessionMutationInFlight.current = true;
    dispatch({ type: "loading", value: true });
    try {
      const result = await api.forkSession(sessionId);
      if (generation !== navigationGeneration.current) return;
      await resumeSession(result.session_id);
    } catch (error) {
      if (generation === navigationGeneration.current) reportError(error);
    } finally {
      sessionMutationInFlight.current = false;
      if (generation === navigationGeneration.current) dispatch({ type: "loading", value: false });
    }
  }, [api, commandRunning, navigationBlocked, notify, reportError, resumeSession, state.loading]);

  const fork = useCallback(async () => {
    if (state.current) await forkSession(state.current.session_id);
  }, [forkSession, state.current]);

  const deleteSession = useCallback(async (sessionId: string) => {
    const deletingCurrent = state.current?.session_id === sessionId;
    if (deletingCurrent && navigationBlocked) {
      notify(state.loading
        ? "Wait for the active session operation before deleting this session."
        : commandRunning
          ? "Wait for the active command before deleting this session."
          : "Finish or interrupt the active turn before deleting this session.");
      return;
    }
    if (sessionMutationInFlight.current) {
      notify("Another session operation is still running.");
      return;
    }
    const generation = navigationGeneration.current;
    sessionMutationInFlight.current = true;
    dispatch({ type: "loading", value: true });
    try {
      await api.deleteSession(sessionId);
      if (generation !== navigationGeneration.current) return;
      sessionCatalog.remove(sessionId);
      await workspaceManager.refresh();
      if (generation !== navigationGeneration.current) return;
      if (deletingCurrent) {
        ++navigationGeneration.current;
        resetStreamingState();
        setCommands([]);
        setNotification("");
      }
      dispatch({ type: "session_deleted", sessionId });
    } catch (error) {
      if (generation === navigationGeneration.current) reportError(error);
    } finally {
      sessionMutationInFlight.current = false;
      if (generation === navigationGeneration.current) dispatch({ type: "loading", value: false });
    }
  }, [api, commandRunning, navigationBlocked, notify, reportError, resetStreamingState, sessionCatalog, state.current, state.loading, workspaceManager]);

  const renameSession = useCallback(async (sessionId: string, title: string) => {
    try {
      await sessionCatalog.rename(sessionId, title);
    } catch (error) {
      reportError(error);
    }
  }, [reportError, sessionCatalog]);

  const runServerCommand = useCallback(async (
    command: CommandInfo,
    raw: string,
  ): Promise<CommandResultData | null> => {
    const current = state.current;
    const generation = navigationGeneration.current;
    if (!current) return null;
    if (state.turnRunning) {
      notify("Finish or interrupt the active turn before running a command.");
      return null;
    }
    if (commandInFlight.current) {
      notify("Another command is still running.");
      return null;
    }
    if (command.kind === "prompt") {
      await sendMessage(raw);
      return null;
    }
    commandInFlight.current = true;
    setCommandRunning(true);
    try {
      const result = await api.runCommand(current.session_id, current.thread_id, command.name, raw);
      if (generation !== navigationGeneration.current) return null;
      if (result.data.status === "error") return result.data;
      const effects = new Set(result.data.effects);
      try {
        const [history, thread, agents, tasks, availableCommands] = await Promise.all([
          effects.has("history") ? api.listMessages(
            current.session_id,
            current.thread_id,
            { limit: 160 },
          ) : null,
          effects.has("thread") ? api.getThread(current.session_id, current.thread_id) : null,
          effects.has("agents") ? api.listAgents(current.session_id, current.thread_id) : null,
          effects.has("tasks") ? api.listTasks(current.session_id, current.thread_id) : null,
          effects.has("commands") ? api.listCommands(current.session_id, current.thread_id) : null,
          effects.has("sessions") ? sessionCatalog.refresh() : null,
        ]);
        if (generation !== navigationGeneration.current) return null;
        if (history) dispatch({
          type: "history",
          history: history.messages,
          nextCursor: history.next_cursor,
        });
        if (thread) {
          dispatch({
            type: "threads",
            threads: state.threads.map((item) => item.thread_id === thread.thread_id ? thread : item),
          });
          dispatch({ type: "thread_synced", thread });
        }
        if (agents) dispatch({ type: "agents", agents });
        if (tasks) dispatch({ type: "tasks", tasks });
        if (availableCommands) setCommands(availableCommands);
      } catch (error) {
        if (generation === navigationGeneration.current) {
          reportError(new Error(`/${command.name} completed, but Web state refresh failed: ${error instanceof Error ? error.message : String(error)}`));
        }
        return result.data;
      }
      return result.data;
    } catch (error) {
      if (generation === navigationGeneration.current) reportError(error);
      return null;
    } finally {
      commandInFlight.current = false;
      setCommandRunning(false);
    }
  }, [api, notify, reportError, sendMessage, sessionCatalog, state.current, state.threads, state.turnRunning]);

  const stopTask = useCallback(async (taskId: string) => {
    if (!state.current) return;
    const generation = navigationGeneration.current;
    try {
      const result = await api.stopTask(state.current.session_id, state.current.thread_id, taskId);
      if (generation !== navigationGeneration.current) return;
      dispatch({ type: "tasks", tasks: result.tasks });
    } catch (error) {
      if (generation === navigationGeneration.current) reportError(error);
    }
  }, [api, reportError, state.current]);

  const stopAllTasks = useCallback(async () => {
    if (!state.current) return;
    const generation = navigationGeneration.current;
    try {
      const result = await api.stopAllTasks(state.current.session_id, state.current.thread_id);
      if (generation !== navigationGeneration.current) return;
      dispatch({ type: "tasks", tasks: result.tasks });
    } catch (error) {
      if (generation === navigationGeneration.current) reportError(error);
    }
  }, [api, reportError, state.current]);

  return {
    state,
    sessions: sessions.items,
    workspaces: workspaces.items,
    archivedSessionIds: workspaces.archivedSessionIds,
    commands,
    commandRunning,
    notification,
    createSession,
    resumeSession,
    selectThread,
    sendMessage,
    updatePendingInput,
    retryLast,
    loadEarlier,
    interrupt,
    resolveInteraction,
    selectAgent,
    selectProvider,
    selectEffort,
    undo,
    clear,
    fork,
    forkSession,
    deleteSession,
    renameSession,
    renameWorkspace,
    deleteWorkspace,
    moveWorkspace,
    moveSession,
    setSessionArchived,
    runServerCommand,
    stopTask,
    stopAllTasks,
    refreshSessions,
    listDirectories,
    clearNotification: () => setNotification(""),
    clearError: () => dispatch({ type: "clear_error" }),
  };
}

async function openSubagentThread(
  api: XBotApi,
  currentThreadId: string,
  sessionId: string,
  thread: ThreadSummary,
): Promise<OpenSessionResponse> {
  if (currentThreadId !== thread.parent_thread_id) {
    await api.openSession({ sessionId, threadId: thread.parent_thread_id, mode: "resume" });
  }
  return api.openThread(sessionId, thread);
}
