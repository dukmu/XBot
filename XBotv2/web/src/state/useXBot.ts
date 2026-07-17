import { useCallback, useEffect, useMemo, useReducer, useRef } from "react";
import { XBotApi, XBotApiError } from "../api/client";
import type { InteractionRequest, OpenSessionResponse, ServerEvent, ThreadSummary } from "../api/types";
import { initialRuntimeState, runtimeReducer } from "./runtime";

const apiBase = import.meta.env.VITE_XBOT_API_BASE || "/api";

export function useXBot() {
  const api = useMemo(() => new XBotApi(apiBase), []);
  const [state, dispatch] = useReducer(runtimeReducer, initialRuntimeState);
  const eventController = useRef<AbortController | null>(null);
  const messageControllers = useRef(new Set<AbortController>());
  const taskTimers = useRef(new Map<string, number>());
  const threadRefreshTimer = useRef<number | null>(null);

  const reportError = useCallback((error: unknown) => {
    if (error instanceof DOMException && error.name === "AbortError") return;
    const message = error instanceof XBotApiError
      ? `${error.code}: ${error.message}`
      : error instanceof Error ? error.message : String(error);
    dispatch({ type: "error", message });
  }, []);

  const handleEvent = useCallback((event: ServerEvent) => {
    dispatch({ type: "event", event });
    if (event.type === "task_updated") {
      const taskId = String(event.data.task_id || "");
      const status = String(event.data.status || "");
      const existing = taskTimers.current.get(taskId);
      if (existing) window.clearTimeout(existing);
      if (taskId && (status === "completed" || status === "stopped")) {
        taskTimers.current.set(taskId, window.setTimeout(() => {
          dispatch({ type: "remove_task", taskId });
          taskTimers.current.delete(taskId);
        }, 4000));
      }
      if (event.data.kind === "agent" && event.session_id) {
        if (threadRefreshTimer.current) window.clearTimeout(threadRefreshTimer.current);
        threadRefreshTimer.current = window.setTimeout(() => {
          void api.listThreads(event.session_id)
            .then((threads) => dispatch({ type: "threads", threads }))
            .catch(reportError);
        }, 250);
      }
    }
  }, [api, reportError]);

  const startEventStream = useCallback((session: OpenSessionResponse) => {
    eventController.current?.abort();
    const controller = new AbortController();
    eventController.current = controller;
    void (async () => {
      try {
        for await (const event of api.streamEvents(
          session.session_id,
          session.thread_id,
          controller.signal,
        )) {
          handleEvent(event);
        }
        if (!controller.signal.aborted) dispatch({ type: "connected", value: false });
      } catch (error) {
        if (!controller.signal.aborted) {
          dispatch({ type: "connected", value: false });
          reportError(error);
        }
      }
    })();
  }, [api, handleEvent, reportError]);

  const activate = useCallback(async (session: OpenSessionResponse) => {
    dispatch({ type: "opened", session });
    const [threads, agents, tasks] = await Promise.all([
      api.listThreads(session.session_id),
      api.listAgents(session.session_id, session.thread_id),
      api.listTasks(session.session_id, session.thread_id),
    ]);
    dispatch({ type: "threads", threads });
    dispatch({ type: "agents", agents });
    dispatch({ type: "tasks", tasks });
    startEventStream(session);
  }, [api, startEventStream]);

  const refreshSessions = useCallback(async () => {
    dispatch({ type: "sessions", sessions: await api.listSessions() });
  }, [api]);

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
        dispatch({ type: "connected", value: true });
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
    };
  }, [api, reportError]);

  const createSession = useCallback(async (workspaceRoot: string) => {
    dispatch({ type: "loading", value: true });
    try {
      const session = await api.openSession({
        workspaceRoot: workspaceRoot.trim() || undefined,
        mode: "new",
      });
      await activate(session);
      await refreshSessions();
    } catch (error) {
      reportError(error);
    } finally {
      dispatch({ type: "loading", value: false });
    }
  }, [activate, api, refreshSessions, reportError]);

  const resumeSession = useCallback(async (sessionId?: string) => {
    const id = sessionId || state.current?.session_id;
    if (!id) return;
    dispatch({ type: "loading", value: true });
    try {
      const session = await api.openSession({ sessionId: id, mode: "resume" });
      await activate(session);
      await refreshSessions();
    } catch (error) {
      reportError(error);
    } finally {
      dispatch({ type: "loading", value: false });
    }
  }, [activate, api, refreshSessions, reportError, state.current?.session_id]);

  const selectThread = useCallback(async (thread: ThreadSummary) => {
    if (!state.current || thread.thread_id === state.current.thread_id) return;
    dispatch({ type: "loading", value: true });
    try {
      const session = thread.kind === "main"
        ? await api.openSession({ sessionId: state.current.session_id, mode: "resume" })
        : await openSubagentThread(api, state.current.thread_id, state.current.session_id, thread);
      await activate(session);
    } catch (error) {
      reportError(error);
    } finally {
      dispatch({ type: "loading", value: false });
    }
  }, [activate, api, reportError, state.current]);

  const sendMessage = useCallback(async (rawContent: string) => {
    const current = state.current;
    const content = rawContent.trim();
    if (!current || !content) return;
    dispatch({ type: "user_message", content });
    const controller = new AbortController();
    messageControllers.current.add(controller);
    try {
      for await (const event of api.sendMessage(
        current.session_id,
        current.thread_id,
        content,
        controller.signal,
      )) {
        handleEvent(event);
      }
    } catch (error) {
      reportError(error);
    } finally {
      messageControllers.current.delete(controller);
    }
  }, [api, handleEvent, reportError, state.current]);

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
    interrupt,
    resolveInteraction,
    selectAgent,
    selectProvider,
    undo,
    clear,
    fork,
    stopTask,
    stopAllTasks,
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
    await api.openSession({ sessionId, mode: "resume" });
  }
  return api.openThread(sessionId, thread);
}
