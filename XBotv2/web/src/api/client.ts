import { decodeSseStream } from "./sse";
import {
  PROTOCOL_VERSION,
  type AgentInfo,
  type HistoryItem,
  type OpenSessionResponse,
  type ProviderInfo,
  type ServerEvent,
  type SessionSummary,
  type TaskData,
  type ThreadSummary,
  type XBotErrorBody,
} from "./types";

export class XBotApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message: string,
    readonly retryable = false,
  ) {
    super(message);
    this.name = "XBotApiError";
  }
}

export class XBotApi {
  constructor(private readonly baseUrl = "/api") {}

  hello(sessionId?: string, threadId = "agent") {
    return this.request<{ server_name: string; protocol_version: string }>(
      "POST",
      "/hello",
      {
        protocol_version: PROTOCOL_VERSION,
        client_name: "xbotv2-web",
        session_id: sessionId || null,
        thread_id: threadId,
      },
    );
  }

  async listSessions(): Promise<SessionSummary[]> {
    const result = await this.request<{ sessions: SessionSummary[] }>("GET", "/sessions");
    return result.sessions;
  }

  async listProviders(): Promise<{ default: string; providers: ProviderInfo[] }> {
    return this.request("GET", "/providers");
  }

  openSession(options: {
    sessionId?: string;
    workspaceRoot?: string;
    mode: "new" | "resume";
    agent?: string;
  }): Promise<OpenSessionResponse> {
    return this.request("POST", "/sessions", {
      session_id: options.sessionId || null,
      thread_id: "agent",
      workspace_root: options.workspaceRoot || null,
      mode: options.mode,
      agent: options.agent || null,
    });
  }

  async listThreads(sessionId: string): Promise<ThreadSummary[]> {
    const result = await this.request<{ threads: ThreadSummary[] }>(
      "GET",
      `/sessions/${segment(sessionId)}/threads`,
    );
    return result.threads;
  }

  openThread(sessionId: string, thread: ThreadSummary): Promise<OpenSessionResponse> {
    return this.request("POST", `/sessions/${segment(sessionId)}/threads`, {
      thread_id: thread.thread_id,
      parent_thread_id: thread.parent_thread_id || "agent",
      workspace_root: null,
      mode: "resume",
      agent: null,
    });
  }

  async listAgents(sessionId: string, threadId: string): Promise<AgentInfo[]> {
    const result = await this.request<{ active: string; agents: AgentInfo[] }>(
      "GET",
      `${threadPath(sessionId, threadId)}/agents`,
    );
    return result.agents;
  }

  selectAgent(sessionId: string, threadId: string, name: string) {
    return this.request<{
      agent: string;
      provider: string;
      model: string;
      model_mode: string;
      context_window: number;
    }>("PUT", `${threadPath(sessionId, threadId)}/agent`, { name });
  }

  selectProvider(sessionId: string, threadId: string, name: string) {
    return this.request<{ provider: string; model: string; model_mode: string }>(
      "PUT",
      `${threadPath(sessionId, threadId)}/provider`,
      { name },
    );
  }

  async listMessages(sessionId: string, threadId: string): Promise<HistoryItem[]> {
    const result = await this.request<{ messages: HistoryItem[] }>(
      "GET",
      `${threadPath(sessionId, threadId)}/messages`,
    );
    return result.messages;
  }

  async listTasks(sessionId: string, threadId: string): Promise<TaskData[]> {
    const result = await this.request<{ tasks: TaskData[] }>(
      "GET",
      `${threadPath(sessionId, threadId)}/tasks`,
    );
    return result.tasks;
  }

  clearHistory(sessionId: string, threadId: string) {
    return this.request<{ messages: HistoryItem[] }>(
      "POST",
      `${threadPath(sessionId, threadId)}/history/clear`,
    );
  }

  undoHistory(sessionId: string, threadId: string, count = 1) {
    return this.request<{ messages: HistoryItem[]; removed_turns: number }>(
      "POST",
      `${threadPath(sessionId, threadId)}/history/undo`,
      { count },
    );
  }

  forkSession(sessionId: string) {
    return this.request<{ session_id: string; source_session_id: string }>(
      "POST",
      `/sessions/${segment(sessionId)}/fork`,
    );
  }

  interrupt(sessionId: string, threadId: string) {
    return this.request<{ cancelled: boolean; status: string }>(
      "POST",
      `${threadPath(sessionId, threadId)}/interrupt`,
    );
  }

  stopTask(sessionId: string, threadId: string, taskId: string) {
    return this.request<{ tasks: TaskData[] }>(
      "POST",
      `${threadPath(sessionId, threadId)}/tasks/${segment(taskId)}/stop`,
    );
  }

  stopAllTasks(sessionId: string, threadId: string) {
    return this.request<{ tasks: TaskData[] }>(
      "POST",
      `${threadPath(sessionId, threadId)}/tasks/stop`,
    );
  }

  respondPermission(
    sessionId: string,
    threadId: string,
    requestId: string,
    decision: "allow" | "deny",
    scope: "once" | "session",
  ) {
    return this.request("POST", `${threadPath(sessionId, threadId)}/interactions/permission-response`, {
      request_id: requestId,
      decision,
      scope,
    });
  }

  respondUserInput(sessionId: string, threadId: string, requestId: string, answer: unknown) {
    return this.request("POST", `${threadPath(sessionId, threadId)}/interactions/user-input`, {
      request_id: requestId,
      answer,
    });
  }

  sendMessage(
    sessionId: string,
    threadId: string,
    content: string,
    signal?: AbortSignal,
  ): AsyncGenerator<ServerEvent> {
    return this.stream("POST", `${threadPath(sessionId, threadId)}/messages`, {
      content,
      request_id: crypto.randomUUID(),
    }, signal);
  }

  streamEvents(sessionId: string, threadId: string, signal?: AbortSignal) {
    return this.stream("GET", `${threadPath(sessionId, threadId)}/events`, undefined, signal);
  }

  private async request<T>(method: string, path: string, body?: unknown): Promise<T> {
    const response = await fetch(`${this.baseUrl}${path}`, {
      method,
      headers: body === undefined ? undefined : { "Content-Type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    if (!response.ok) await throwResponse(response);
    return response.json() as Promise<T>;
  }

  private async *stream(
    method: string,
    path: string,
    body?: unknown,
    signal?: AbortSignal,
  ): AsyncGenerator<ServerEvent> {
    const response = await fetch(`${this.baseUrl}${path}`, {
      method,
      signal,
      headers: {
        Accept: "text/event-stream",
        ...(body === undefined ? {} : { "Content-Type": "application/json" }),
      },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    if (!response.ok) await throwResponse(response);
    if (!response.body) throw new Error("XBot returned an empty event stream");
    for await (const event of decodeSseStream(response.body)) {
      yield event;
      if (event.type === "end") return;
    }
  }
}

async function throwResponse(response: Response): Promise<never> {
  let body: XBotErrorBody | null = null;
  try {
    body = (await response.json()) as XBotErrorBody;
  } catch {
    // Preserve the HTTP status when a proxy returns a non-JSON failure.
  }
  throw new XBotApiError(
    response.status,
    body?.code || String(response.status),
    body?.message || response.statusText || "XBot request failed",
    body?.retryable || false,
  );
}

function segment(value: string): string {
  return encodeURIComponent(value);
}

function threadPath(sessionId: string, threadId: string): string {
  return `/sessions/${segment(sessionId)}/threads/${segment(threadId)}`;
}
