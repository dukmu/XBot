import { useCallback, useEffect, useMemo, useReducer, useRef } from "react";
import { XBotApi, XBotApiError } from "../api/client";
import type { InteractionRequest, OpenSessionResponse, ServerEvent, TaskData, ThreadSummary } from "../api/types";
import type { PendingAttachment } from "../components/Composer";
import { initialRuntimeState, runtimeReducer } from "./runtime";

const apiBase = import.meta.env.VITE_XBOT_API_BASE || "/api";

export function useXBot() {
  const api = useMemo(() => new XBotApi(apiBase), []);
  const [state, dispatch] = useReducer(runtimeReducer, initialRuntimeState);
  const eventController = useRef<AbortController | null>(null);
  const messageControllers = useRef(new Set<AbortController>());
  const taskTimers = useRef(new Map<string, number>());
  const threadRefreshTimer = useRef<number | null>(null);
  const streamEvents = useRef<ServerEvent[]>([]);
  const streamFlushTimer = useRef<number | null>(null);
  const navigationGeneration = useRef(0);

  const resetStreamingState = useCallback(() => {
    eventController.current?.abort();
    eventController.current = null;
    if (streamFlushTimer.current !== null) {
      window.clearTimeout(streamFlushTimer.current);
      streamFlushTimer.current = null;
    }
    streamEvents.current = [];
    for (const timer of taskTimers.current.values()) window.clearTimeout(timer);
    taskTimers.current.clear();
    if (threadRefreshTimer.current !== null) {
      window.clearTimeout(threadRefreshTimer.current);
      threadRefreshTimer.current = null;
    }
  }, []);

  const flushStreamEvents = useCallback(() => {
    streamFlushTimer.current = null;
    const pending = streamEvents.current;
    streamEvents.current = [];
    if (pending.length > 0) dispatch({ type: "events", events: pending });
  }, []);

  const queueStreamEvent = useCallback((event: ServerEvent) => {
    streamEvents.current.push(event);
    if (streamFlushTimer.current === null) {
      streamFlushTimer.current = window.setTimeout(flushStreamEvents, 16);
    }
  }, [flushStreamEvents]);

  const reportError = useCallback((error: unknown, turnFailed = false) => {
    if (error instanceof DOMException && error.name === "AbortError") return;
    const message = error instanceof XBotApiError
      ? `${error.code}: ${error.message}`
      : error instanceof Error ? error.message : String(error);
    dispatch({ type: turnFailed ? "turn_error" : "error", message });
  }, []);

  const handleEvent = useCallback((event: ServerEvent, generation?: number) => {
    if (generation !== undefined && generation !== navigationGeneration.current) return;
    const eventGeneration = generation ?? navigationGeneration.current;
    const isHighFrequencyDelta = event.type === "assistant_message_delta" || event.type === "tool_call_delta";
    if (isHighFrequencyDelta) queueStreamEvent(event);
    else {
      if (streamFlushTimer.current !== null) {
        window.clearTimeout(streamFlushTimer.current);
        flushStreamEvents();
      }
      dispatch({ type: "event", event });
    }
    if (event.type === "task_updated") {
      const taskId = String(event.data.task_id || "");
      const status = String(event.data.status || "");
      const existing = taskTimers.current.get(taskId);
      if (existing) window.clearTimeout(existing);
      if (taskId && (status === "completed" || status === "stopped")) {
        taskTimers.current.set(taskId, window.setTimeout(() => {
          if (eventGeneration !== navigationGeneration.current) return;
          dispatch({ type: "remove_task", taskId });
          taskTimers.current.delete(taskId);
        }, 4000));
      }
      if (event.data.kind === "agent" && event.session_id) {
        if (threadRefreshTimer.current) window.clearTimeout(threadRefreshTimer.current);
        threadRefreshTimer.current = window.setTimeout(() => {
          if (eventGeneration !== navigationGeneration.current) return;
          void api.listThreads(event.session_id)
            .then((threads) => {
              if (eventGeneration === navigationGeneration.current) dispatch({ type: "threads", threads });
            })
            .catch(reportError);
        }, 250);
      }
    }
  }, [api, flushStreamEvents, queueStreamEvent, reportError]);

  const startEventStream = useCallback((session: OpenSessionResponse, generation: number) => {
    eventController.current?.abort();
    const controller = new AbortController();
    eventController.current = controller;
    dispatch({ type: "event_stream", value: true });
    void (async () => {
      try {
        for await (const event of api.streamEvents(
          session.session_id,
          session.thread_id,
          controller.signal,
        )) {
          handleEvent(event, generation);
        }
        if (!controller.signal.aborted && generation === navigationGeneration.current) dispatch({ type: "event_stream", value: false });
      } catch (error) {
        if (!controller.signal.aborted && generation === navigationGeneration.current) {
          dispatch({ type: "event_stream", value: false });
          reportError(error, true);
        }
      }
    })();
  }, [api, handleEvent, reportError]);

  const activate = useCallback(async (session: OpenSessionResponse, generation: number) => {
    if (generation !== navigationGeneration.current) return;
    let resources: [ThreadSummary[], Awaited<ReturnType<XBotApi["listAgents"]>>, TaskData[]];
    try {
      resources = await Promise.all([
        api.listThreads(session.session_id),
        api.listAgents(session.session_id, session.thread_id),
        api.listTasks(session.session_id, session.thread_id),
      ]);
    } catch (error) {
      throw error;
    }
    const [threads, agents, tasks] = resources;
    if (generation !== navigationGeneration.current) return;
    resetStreamingState();
    dispatch({ type: "opened", session });
    dispatch({ type: "threads", threads });
    dispatch({ type: "agents", agents });
    dispatch({ type: "tasks", tasks });
    startEventStream(session, generation);
  }, [api, resetStreamingState, startEventStream]);

  const refreshSessions = useCallback(async () => {
    try {
      dispatch({ type: "sessions", sessions: await api.listSessions() });
    } catch (error) {
      reportError(error);
    }
  }, [api, reportError]);

  useEffect(() => {
    let alive = true;
    void (async () => {
      try {
        await api.hello();
        const [sessions, providers] = await Promise.all([
          api.listSessions(),
          api.listProviders(),
        ]);
        if (!alive) return;
        dispatch({ type: "sessions", sessions });
        dispatch({ type: "providers", providers: providers.providers });
        dispatch({ type: "server_reachable", value: true });
        dispatch({ type: "loading", value: false });
      } catch (error) {
        if (alive) reportError(error);
      }
    })();
    return () => {
      alive = false;
      eventController.current?.abort();
      for (const controller of messageControllers.current) controller.abort();
      for (const timer of taskTimers.current.values()) window.clearTimeout(timer);
      if (threadRefreshTimer.current) window.clearTimeout(threadRefreshTimer.current);
      if (streamFlushTimer.current) window.clearTimeout(streamFlushTimer.current);
      streamEvents.current = [];
    };
  }, [api, reportError]);

  const createSession = useCallback(async (workspaceRoot: string) => {
    const previous = state.current;
    const generation = ++navigationGeneration.current;
    dispatch({ type: "loading", value: true });
    try {
      const session = await api.openSession({
        workspaceRoot: workspaceRoot.trim() || undefined,
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
  }, [activate, api, refreshSessions, reportError, startEventStream, state.current]);

  const resumeSession = useCallback(async (sessionId?: string) => {
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
        workspaceRoot: mainThread.workspace_root,
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
  }, [activate, api, refreshSessions, reportError, startEventStream, state.current]);

  const selectThread = useCallback(async (thread: ThreadSummary) => {
    if (!state.current || thread.thread_id === state.current.thread_id) return;
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
  }, [activate, api, reportError, startEventStream, state.current]);

  const sendMessage = useCallback(async (rawContent: string, attachments: PendingAttachment[] = []) => {
    const current = state.current;
    const generation = navigationGeneration.current;
    const content = rawContent.trim();
    if (!current || (!content && attachments.length === 0)) return;
    dispatch({
      type: "user_message",
      content,
      images: attachments.map((attachment) => ({ label: attachment.name, src: attachment.preview })),
    });
    const controller = new AbortController();
    messageControllers.current.add(controller);
    try {
      for await (const event of api.sendMessage(
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
      )) {
        handleEvent(event, generation);
      }
    } catch (error) {
      if (generation === navigationGeneration.current) reportError(error, true);
    } finally {
      messageControllers.current.delete(controller);
    }
  }, [api, handleEvent, reportError, state.current]);

  const retryLast = useCallback(async () => {
    if (state.turnRunning || !state.current) return;
    const lastUser = [...state.entries].reverse().find(
      (entry) => entry.kind === "message" && entry.role === "user" && entry.content.trim(),
    );
    if (!lastUser || lastUser.kind !== "message") return;
    await sendMessage(lastUser.content);
  }, [sendMessage, state.current, state.entries, state.turnRunning]);

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
    try {
      const result = await api.selectAgent(state.current.session_id, state.current.thread_id, name);
      dispatch({
        type: "agent_selected",
        agent: result.agent,
        provider: result.provider,
        model: result.model,
        modelMode: result.model_mode,
        contextWindow: result.context_window,
      });
    } catch (error) {
      reportError(error);
    }
  }, [api, reportError, state.current, state.turnRunning]);

  const selectProvider = useCallback(async (name: string) => {
    if (!state.current || state.turnRunning) return;
    try {
      const result = await api.selectProvider(state.current.session_id, state.current.thread_id, name);
      dispatch({
        type: "provider_selected",
        provider: result.provider,
        model: result.model,
        modelMode: result.model_mode,
      });
    } catch (error) {
      reportError(error);
    }
  }, [api, reportError, state.current, state.turnRunning]);

  const undo = useCallback(async (count = 1) => {
    if (!state.current || state.turnRunning) return;
    try {
      const result = await api.undoHistory(state.current.session_id, state.current.thread_id, count);
      dispatch({ type: "history", history: result.messages });
    } catch (error) {
      reportError(error);
    }
  }, [api, reportError, state.current, state.turnRunning]);

  const clear = useCallback(async () => {
    if (!state.current || state.turnRunning) return;
    try {
      const result = await api.clearHistory(state.current.session_id, state.current.thread_id);
      dispatch({ type: "history", history: result.messages });
    } catch (error) {
      reportError(error);
    }
  }, [api, reportError, state.current, state.turnRunning]);

  const fork = useCallback(async () => {
    if (!state.current || state.turnRunning) return;
    try {
      const result = await api.forkSession(state.current.session_id);
      await resumeSession(result.session_id);
    } catch (error) {
      reportError(error);
    }
  }, [api, reportError, resumeSession, state.current, state.turnRunning]);

  const stopTask = useCallback(async (taskId: string) => {
    if (!state.current) return;
    try {
      const result = await api.stopTask(state.current.session_id, state.current.thread_id, taskId);
      dispatch({ type: "tasks", tasks: result.tasks });
    } catch (error) {
      reportError(error);
    }
  }, [api, reportError, state.current]);

  const stopAllTasks = useCallback(async () => {
    if (!state.current) return;
    try {
      const result = await api.stopAllTasks(state.current.session_id, state.current.thread_id);
      dispatch({ type: "tasks", tasks: result.tasks });
    } catch (error) {
      reportError(error);
    }
  }, [api, reportError, state.current]);

  return {
    state,
    createSession,
    resumeSession,
    selectThread,
    sendMessage,
    retryLast,
    interrupt,
    resolveInteraction,
    selectAgent,
    selectProvider,
    undo,
    clear,
    fork,
    stopTask,
    stopAllTasks,
    refreshSessions,
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
