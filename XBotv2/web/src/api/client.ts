import type { OpenedThread, TrajectoryRead, ServerEvent } from "./wire";
import { decodeSseStream } from "./sse";
import {
  PROTOCOL_VERSION,
  type AgentInfo,
  type AttachmentInput,
  type CommandInfo,
  type CommandResult,
  type DirectoryListingData,
  type MediaInput,
  type JsonObject,
  type MessageRequest,
  type PendingInput,
  type PluginConfigCatalog,
  type PluginConfigScope,
  type ProviderInfo,
  type RegenerateRequest,
  type SessionListData,
  type SessionPolicy,
  type SessionPolicyPatch,
  type SessionSummary,
  type JobData,
  type ThreadSummary,
  type ToolInfo,
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

/**
 * Whether an error means the session or thread this client is attached to no
 * longer exists.  Callers recover by re-opening the session: the request was a
 * background refresh the user never asked for, so a banner over a working chat
 * would only report noise.
 */
export function isMissingSessionError(error: unknown): boolean {
  return error instanceof XBotApiError && (error.status === 404 || error.code === "not_found");
}

export class XBotApi {
  constructor(private readonly baseUrl = "/api") {}

  hello(sessionId?: string, threadId = "main") {
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

  listDirectories(path?: string, signal?: AbortSignal): Promise<DirectoryListingData> {
    const query = path ? `?path=${encodeURIComponent(path)}` : "";
    return this.request("GET", `/directories${query}`, undefined, signal);
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
  }): Promise<OpenedThread> {
    const result = await this.request<{ data: OpenedThread }>("POST", "/sessions", {
      session_id: options.sessionId || null,
      thread_id: options.threadId || "main",
      workspace_root: options.workspaceRoot || null,
      mode: options.mode,
      agent: options.agent || null,
      history_limit: options.historyLimit ?? 160,
    });
    return result.data;
  }

  async listThreads(sessionId: string): Promise<ThreadSummary[]> {
    const result = await this.request<{ threads: ThreadSummary[] }>(
      "GET",
      `/sessions/${segment(sessionId)}/threads`,
    );
    return result.threads;
  }

  getSessionPolicy(sessionId: string): Promise<SessionPolicy> {
    return this.request("GET", `/sessions/${segment(sessionId)}/policy`);
  }

  updateSessionPolicy(sessionId: string, patch: SessionPolicyPatch): Promise<SessionPolicy> {
    return this.request("PATCH", `/sessions/${segment(sessionId)}/policy`, patch);
  }

  listPluginConfig(
    sessionId: string,
    threadId: string,
    scope: PluginConfigScope,
  ): Promise<PluginConfigCatalog> {
    return this.request(
      "GET",
      `${threadPath(sessionId, threadId)}/plugin-config?scope=${scope}`,
    );
  }

  updatePluginConfig(
    sessionId: string,
    threadId: string,
    pluginId: string,
    scope: PluginConfigScope,
    revision: string,
    config: JsonObject,
  ): Promise<PluginConfigCatalog> {
    return this.request(
      "PATCH",
      `${threadPath(sessionId, threadId)}/plugin-config/${segment(pluginId)}?scope=${scope}`,
      { revision, config },
    );
  }

  getThread(sessionId: string, threadId: string): Promise<ThreadSummary> {
    return this.request("GET", `${threadPath(sessionId, threadId)}`);
  }

  async openThread(sessionId: string, thread: ThreadSummary): Promise<OpenedThread> {
    const result = await this.request<{ data: OpenedThread }>("POST", `/sessions/${segment(sessionId)}/threads`, {
      thread_id: thread.thread_id,
      parent_thread_id: thread.parent_thread_id || "main",
      workspace_root: null,
      mode: "resume",
      agent: null,
      history_limit: 160,
    });
    return result.data;
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

  listMessages(
    sessionId: string,
    threadId: string,
    options: { cursor?: string; limit?: number } = {},
  ): Promise<import("./wire").HistoryPage<import("./wire").ConversationRecord>> {
    const query = new URLSearchParams();
    if (options.cursor) query.set("cursor", options.cursor);
    if (options.limit) query.set("limit", String(options.limit));
    return this.request("GET", `${threadPath(sessionId, threadId)}/messages${query.size ? `?${query}` : ""}`);
  }

  listTrajectory(
    sessionId: string,
    threadId: string,
    options: { cursor?: string; before?: number; limit?: number } = {},
  ): Promise<TrajectoryRead> {
    const query = new URLSearchParams();
    if (options.cursor) query.set("cursor", options.cursor);
    if (options.before) query.set("before", String(options.before));
    if (options.limit) query.set("limit", String(options.limit));
    return this.request("GET", `${threadPath(sessionId, threadId)}/trajectory${query.size ? `?${query}` : ""}`);
  }

  async listJobs(sessionId: string, threadId: string): Promise<JobData[]> {
    const result = await this.request<{ jobs: JobData[] }>(
      "GET",
      `${threadPath(sessionId, threadId)}/jobs`,
    );
    return result.jobs;
  }

  listTodos(sessionId: string, threadId: string): Promise<import("./wire").TaskList> {
    return this.request("GET", `${threadPath(sessionId, threadId)}/todos`);
  }

  async listCommands(sessionId: string, threadId: string): Promise<CommandInfo[]> {
    const result = await this.request<{ commands: CommandInfo[] }>(
      "GET",
      `${threadPath(sessionId, threadId)}/commands`,
    );
    return result.commands;
  }

  runCommand(sessionId: string, threadId: string, _command: string, raw: string) {
    return this.request<CommandResult>(
      "POST",
      `${threadPath(sessionId, threadId)}/commands`,
      { raw },
    );
  }

  clearHistory(sessionId: string, threadId: string) {
    return this.request<import("./wire").HistoryMutation>(
      "POST",
      `${threadPath(sessionId, threadId)}/history/clear`,
    );
  }

  undoHistory(sessionId: string, threadId: string, count = 1) {
    return this.request<import("./wire").HistoryMutation>(
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

  stopJob(sessionId: string, threadId: string, jobId: string) {
    return this.request<{ jobs: JobData[] }>(
      "POST",
      `${threadPath(sessionId, threadId)}/jobs/${segment(jobId)}/stop`,
    );
  }

  stopAllJobs(sessionId: string, threadId: string) {
    return this.request<{ jobs: JobData[] }>(
      "POST",
      `${threadPath(sessionId, threadId)}/jobs/stop`,
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

  /** Tool catalog for the active thread: namespace tells a skill tool apart. */
  listTools(sessionId: string, threadId: string) {
    return this.request<{ tools: ToolInfo[] }>(
      "GET",
      `${threadPath(sessionId, threadId)}/tools`,
    );
  }

  updatePendingInput(
    sessionId: string,
    threadId: string,
    messageId: string,
    action: { action: "edit"; content: string } | { action: "remove" | "steer" },
  ) {
    return this.request<{ items: PendingInput[] }>(
      "PATCH",
      `${threadPath(sessionId, threadId)}/queue/${encodeURIComponent(messageId)}`,
      action,
    );
  }

  sendMessage(
    sessionId: string,
    threadId: string,
    content: string,
    media: MediaInput[],
    attachments: AttachmentInput[],
    signal?: AbortSignal,
    requestId = crypto.randomUUID(),
    delivery: "queue" | "steer" = "steer",
  ): Promise<void> {
    const payload: MessageRequest = {
      content,
      media,
      attachments,
      request_id: requestId,
      delivery,
    };
    return this.requestNoContent("POST", `${threadPath(sessionId, threadId)}/messages`, payload, signal);
  }

  regenerateMessage(
    sessionId: string,
    threadId: string,
    signal?: AbortSignal,
    requestId = crypto.randomUUID(),
  ): Promise<void> {
    const payload: RegenerateRequest = { request_id: requestId };
    return this.requestNoContent(
      "POST",
      `${threadPath(sessionId, threadId)}/history/regenerate`,
      payload,
      signal,
    );
  }

  private async requestNoContent(
    method: string,
    path: string,
    body?: MessageRequest | RegenerateRequest,
    signal?: AbortSignal,
  ): Promise<void> {
    const response = await fetch(`${this.baseUrl}${path}`, {
      method,
      signal,
      headers: {
        ...(body === undefined ? {} : { "Content-Type": "application/json" }),
      },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    if (!response.ok) await throwResponse(response);
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
      yield event;
    }
  }

  streamWorkspaceEvents(after: number, signal?: AbortSignal): AsyncGenerator<ServerEvent> {
    return this.stream("GET", `/workspaces/events?after=${encodeURIComponent(after)}`, undefined, signal);
  }

  private async request<T>(
    method: string,
    path: string,
    body?: unknown,
    signal?: AbortSignal,
  ): Promise<T> {
    const response = await fetch(`${this.baseUrl}${path}`, {
      method,
      signal,
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
      if (event.kind === "end") return;
    }
    throw new Error("XBot event stream ended before its terminal event");
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
