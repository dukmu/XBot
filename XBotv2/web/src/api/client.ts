import { decodeSseStream } from "./sse";
import {
  PROTOCOL_VERSION,
  type AgentInfo,
  type AttachmentInput,
  type CommandInfo,
  type CommandResult,
  type HistoryItem,
  type ImageInput,
  type MessagePage,
  type OpenSessionResponse,
  type ProviderInfo,
  type ServerEvent,
  type SessionListData,
  type SessionSummary,
  type TaskData,
  type TodoItemData,
  type ThreadSummary,
  type WorkspaceData,
  type WorkspaceListData,
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

  listSessions(): Promise<SessionListData> {
    return this.request("GET", "/sessions");
  }

  renameSession(sessionId: string, title: string): Promise<SessionSummary> {
    return this.request("PATCH", `/sessions/${segment(sessionId)}`, { title });
  }

  listWorkspaces(): Promise<WorkspaceListData> {
    return this.request("GET", "/workspaces");
  }

  async createWorkspace(path: string): Promise<WorkspaceData> {
    const result = await this.request<{ workspace: WorkspaceData; created: boolean }>(
      "POST",
      "/workspaces",
      { path },
    );
    return result.workspace;
  }

  async renameWorkspace(workspaceId: string, title: string): Promise<WorkspaceData> {
    const result = await this.request<{ workspace: WorkspaceData }>(
      "PATCH",
      `/workspaces/${segment(workspaceId)}`,
      { title },
    );
    return result.workspace;
  }

  async deleteWorkspace(workspaceId: string): Promise<void> {
    await this.request("DELETE", `/workspaces/${segment(workspaceId)}`);
  }

  async reorderWorkspace(workspaceId: string, beforeWorkspaceId: string | null): Promise<string[]> {
    const result = await this.request<{ workspace_ids: string[] }>(
      "POST",
      `/workspaces/${segment(workspaceId)}/order`,
      { before_workspace_id: beforeWorkspaceId },
    );
    return result.workspace_ids;
  }

  async reorderWorkspaceSession(
    workspaceId: string,
    sessionId: string,
    beforeSessionId: string | null,
  ): Promise<WorkspaceData> {
    const result = await this.request<{ workspace: WorkspaceData }>(
      "POST",
      `/workspaces/${segment(workspaceId)}/sessions/${segment(sessionId)}/order`,
      { before_session_id: beforeSessionId },
    );
    return result.workspace;
  }

  async setSessionArchived(sessionId: string, archived: boolean): Promise<string[]> {
    const result = await this.request<{ archived_session_ids: string[] }>(
      archived ? "PUT" : "DELETE",
      `/sessions/${segment(sessionId)}/archive`,
    );
    return result.archived_session_ids;
  }

  async listProviders(): Promise<{ default: string; providers: ProviderInfo[] }> {
    return this.request("GET", "/providers");
  }

  async openSession(options: {
    sessionId?: string;
    threadId?: string;
    workspaceRoot?: string;
    mode: "new" | "resume";
    agent?: string;
    historyLimit?: number;
  }): Promise<OpenSessionResponse> {
    const result = await this.request<OpenSessionResponse>("POST", "/sessions", {
      session_id: options.sessionId || null,
      thread_id: options.threadId || "agent",
      workspace_root: options.workspaceRoot || null,
      mode: options.mode,
      agent: options.agent || null,
      history_limit: options.historyLimit ?? 160,
    });
    return this.withArtifactUrls(result);
  }

  async listThreads(sessionId: string): Promise<ThreadSummary[]> {
    const result = await this.request<{ threads: ThreadSummary[] }>(
      "GET",
      `/sessions/${segment(sessionId)}/threads`,
    );
    return result.threads;
  }

  getThread(sessionId: string, threadId: string): Promise<ThreadSummary> {
    return this.request("GET", `${threadPath(sessionId, threadId)}`);
  }

  async openThread(sessionId: string, thread: ThreadSummary): Promise<OpenSessionResponse> {
    const result = await this.request<OpenSessionResponse>("POST", `/sessions/${segment(sessionId)}/threads`, {
      thread_id: thread.thread_id,
      parent_thread_id: thread.parent_thread_id || "agent",
      workspace_root: null,
      mode: "resume",
      agent: null,
      history_limit: 160,
    });
    return this.withArtifactUrls(result);
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

  selectProvider(sessionId: string, threadId: string, name: string, model?: string) {
    return this.request<{ provider: string; model: string; model_mode: string }>(
      "PUT",
      `${threadPath(sessionId, threadId)}/provider`,
      { name, model: model || null },
    );
  }

  selectEffort(sessionId: string, threadId: string, effort: string) {
    return this.request<{
      provider: string;
      model: string;
      reasoning_effort: string;
      model_mode: string;
      available: string[];
    }>("PUT", `${threadPath(sessionId, threadId)}/effort`, { effort });
  }

  async listMessages(
    sessionId: string,
    threadId: string,
    options: { cursor?: string; limit?: number } = {},
  ): Promise<MessagePage> {
    const query = new URLSearchParams();
    if (options.cursor) query.set("cursor", options.cursor);
    if (options.limit) query.set("limit", String(options.limit));
    const result = await this.request<MessagePage>(
      "GET",
      `${threadPath(sessionId, threadId)}/messages${query.size ? `?${query}` : ""}`,
    );
    return {
      ...result,
      messages: this.decorateHistory(sessionId, threadId, result.messages),
    };
  }

  async listTasks(sessionId: string, threadId: string): Promise<TaskData[]> {
    const result = await this.request<{ tasks: TaskData[] }>(
      "GET",
      `${threadPath(sessionId, threadId)}/tasks`,
    );
    return result.tasks;
  }

  async listTodos(sessionId: string, threadId: string): Promise<TodoItemData[]> {
    const result = await this.request<{ items: TodoItemData[] }>(
      "GET",
      `${threadPath(sessionId, threadId)}/todos`,
    );
    return result.items;
  }

  async listCommands(sessionId: string, threadId: string): Promise<CommandInfo[]> {
    const result = await this.request<{ commands: CommandInfo[] }>(
      "GET",
      `${threadPath(sessionId, threadId)}/commands`,
    );
    return result.commands;
  }

  runCommand(sessionId: string, threadId: string, command: string, raw: string) {
    return this.request<CommandResult>(
      "POST",
      `${threadPath(sessionId, threadId)}/commands`,
      { command, raw, kind: "server" },
    );
  }

  clearHistory(sessionId: string, threadId: string) {
    return this.request<{ messages: HistoryItem[] }>(
      "POST",
      `${threadPath(sessionId, threadId)}/history/clear`,
    );
  }

  undoHistory(sessionId: string, threadId: string, count = 1) {
    return this.request<{ messages: HistoryItem[]; removed_turns: number; history_cursor?: string | null }>(
      "POST",
      `${threadPath(sessionId, threadId)}/history/undo`,
      { count, history_limit: 160 },
    );
  }

  forkSession(sessionId: string) {
    return this.request<{ session_id: string; source_session_id: string }>(
      "POST",
      `/sessions/${segment(sessionId)}/fork`,
    );
  }

  deleteSession(sessionId: string) {
    return this.request<{ session_id: string; status: "deleted" }>(
      "DELETE",
      `/sessions/${segment(sessionId)}`,
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

  async *sendMessage(
    sessionId: string,
    threadId: string,
    content: string,
    images: ImageInput[],
    attachments: AttachmentInput[],
    signal?: AbortSignal,
    requestId = crypto.randomUUID(),
  ): AsyncGenerator<ServerEvent> {
    for await (const event of this.stream("POST", `${threadPath(sessionId, threadId)}/messages`, {
      content,
      images,
      attachments,
      request_id: requestId,
    }, signal)) {
      yield this.withEventArtifactUrls(sessionId, threadId, event);
    }
  }

  async *regenerateMessage(
    sessionId: string,
    threadId: string,
    signal?: AbortSignal,
    requestId = crypto.randomUUID(),
  ): AsyncGenerator<ServerEvent> {
    for await (const event of this.stream(
      "POST",
      `${threadPath(sessionId, threadId)}/history/regenerate`,
      { request_id: requestId },
      signal,
    )) {
      yield this.withEventArtifactUrls(sessionId, threadId, event);
    }
  }

  artifactUrl(sessionId: string, threadId: string, artifactId: string): string {
    const id = artifactId.split("/").map(segment).join("/");
    return `${this.baseUrl}${threadPath(sessionId, threadId)}/artifacts/${id}`;
  }

  async *streamEvents(
    sessionId: string,
    threadId: string,
    after: number,
    signal?: AbortSignal,
  ) {
    for await (const event of this.stream(
      "GET",
      `${threadPath(sessionId, threadId)}/events?after=${encodeURIComponent(after)}`,
      undefined,
      signal,
    )) {
      yield this.withEventArtifactUrls(sessionId, threadId, event);
    }
  }

  streamWorkspaceEvents(after: number, signal?: AbortSignal): AsyncGenerator<ServerEvent> {
    return this.stream("GET", `/workspaces/events?after=${encodeURIComponent(after)}`, undefined, signal);
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
    throw new Error("XBot event stream ended before its terminal event");
  }

  private withArtifactUrls(session: OpenSessionResponse): OpenSessionResponse {
    return {
      ...session,
      history: this.decorateHistory(session.session_id, session.thread_id, session.history),
    };
  }

  private decorateHistory(
    sessionId: string,
    threadId: string,
    history: HistoryItem[],
  ): HistoryItem[] {
    return history.map((item) => ({
      ...item,
      images: item.images.map((image) => ({
        ...image,
        url: this.artifactUrl(sessionId, threadId, image.path),
      })),
      artifacts: item.artifacts.map((artifact) => ({
        ...artifact,
        url: typeof artifact.id === "string"
          ? this.artifactUrl(sessionId, threadId, artifact.id)
          : undefined,
      })),
    }));
  }

  private withEventArtifactUrls(
    sessionId: string,
    threadId: string,
    event: ServerEvent,
  ): ServerEvent {
    if (event.type === "history_updated") {
      const history = Array.isArray(event.data.history)
        ? event.data.history as unknown as HistoryItem[]
        : [];
      return {
        ...event,
        data: {
          ...event.data,
          history: this.decorateHistory(sessionId, threadId, history),
        },
      };
    }
    if (!["message", "tool_result"].includes(event.type)) return event;
    const images = Array.isArray(event.data.images) ? event.data.images : [];
    const artifacts = Array.isArray(event.data.artifacts) ? event.data.artifacts : [];
    return {
      ...event,
      data: {
        ...event.data,
        images: images.map((value) => {
          const image = value && typeof value === "object" ? value as Record<string, unknown> : {};
          return {
            ...image,
            url: typeof image.path === "string"
              ? this.artifactUrl(sessionId, threadId, image.path)
              : undefined,
          };
        }),
        artifacts: artifacts.map((value) => {
          const artifact = value && typeof value === "object" ? value as Record<string, unknown> : {};
          return {
            ...artifact,
            url: typeof artifact.id === "string"
              ? this.artifactUrl(sessionId, threadId, artifact.id)
              : undefined,
          };
        }),
      },
    };
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
