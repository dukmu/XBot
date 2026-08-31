import type { XBotApi } from "../api/client";
import type { ServerEvent, WorkspaceData } from "../api/types";

export interface WorkspaceSnapshot {
  items: readonly WorkspaceData[];
  archivedSessionIds: readonly string[];
  eventCursor: number;
  state: "idle" | "loading" | "error";
  error: unknown;
}

type WorkspaceApi = Pick<
  XBotApi,
  "listWorkspaces" | "createWorkspace" | "renameWorkspace" | "deleteWorkspace" | "reorderWorkspace" | "reorderWorkspaceSession" | "setSessionArchived"
>;

export class WorkspaceManager {
  private items: readonly WorkspaceData[] = [];
  private archivedSessionIds: readonly string[] = [];
  private eventCursor = 0;
  private state: WorkspaceSnapshot["state"] = "idle";
  private error: unknown = null;
  private snapshot: WorkspaceSnapshot = this.buildSnapshot();
  private readonly listeners = new Set<() => void>();
  private readonly removedIds = new Set<string>();
  private refreshPromise: Promise<void> | null = null;
  private refreshEvents: ServerEvent[] | null = null;
  private revision = 0;
  private orderGeneration = 0;

  constructor(private readonly api: WorkspaceApi) {}

  subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  };

  getSnapshot = (): WorkspaceSnapshot => this.snapshot;

  refresh(): Promise<void> {
    if (this.refreshPromise) return this.refreshPromise;
    const revision = this.revision;
    const refreshEvents: ServerEvent[] = [];
    this.refreshEvents = refreshEvents;
    this.state = "loading";
    this.error = null;
    this.publish();
    this.refreshPromise = this.api.listWorkspaces()
      .then((listing) => {
        if (revision === this.revision) {
          this.items = listing.items.filter((workspace) => !this.removedIds.has(workspace.workspace_id));
          this.archivedSessionIds = listing.archived_session_ids;
          this.eventCursor = listing.event_cursor;
          for (const event of refreshEvents) {
            if (event.sequence > listing.event_cursor) this.applyCatalogEvent(event);
          }
        }
        this.state = "idle";
      })
      .catch((error) => {
        this.state = "error";
        this.error = error;
        throw error;
      })
      .finally(() => {
        if (this.refreshEvents === refreshEvents) this.refreshEvents = null;
        this.refreshPromise = null;
        this.publish();
      });
    return this.refreshPromise;
  }

  async create(path: string): Promise<WorkspaceData> {
    const workspace = await this.api.createWorkspace(path);
    this.revision += 1;
    this.removedIds.delete(workspace.workspace_id);
    this.upsert(workspace);
    return workspace;
  }

  async rename(workspaceId: string, title: string): Promise<void> {
    const workspace = await this.api.renameWorkspace(workspaceId, title);
    this.revision += 1;
    this.upsert(workspace);
  }

  async delete(workspaceId: string): Promise<void> {
    await this.api.deleteWorkspace(workspaceId);
    this.revision += 1;
    this.removedIds.add(workspaceId);
    this.items = this.items.filter((workspace) => workspace.workspace_id !== workspaceId);
    this.publish();
  }

  async move(workspaceId: string, direction: -1 | 1): Promise<void> {
    const index = this.items.findIndex((workspace) => workspace.workspace_id === workspaceId);
    const target = index + direction;
    if (index < 0 || target < 0 || target >= this.items.length) return;
    const previous = this.items;
    const generation = ++this.orderGeneration;
    const optimistic = [...this.items];
    const [workspace] = optimistic.splice(index, 1);
    optimistic.splice(target, 0, workspace);
    this.items = optimistic;
    this.publish();
    const before = direction < 0
      ? previous[target].workspace_id
      : previous[index + 2]?.workspace_id || null;
    try {
      const order = await this.api.reorderWorkspace(workspaceId, before);
      if (generation !== this.orderGeneration) return;
      this.revision += 1;
      this.installOrder(order);
    } catch (error) {
      if (generation === this.orderGeneration) {
        this.items = previous;
        this.publish();
      }
      throw error;
    }
  }

  async setSessionArchived(sessionId: string, archived: boolean): Promise<void> {
    this.archivedSessionIds = await this.api.setSessionArchived(sessionId, archived);
    this.revision += 1;
    this.publish();
  }

  async moveSession(
    workspaceId: string,
    sessionId: string,
    direction: -1 | 1,
  ): Promise<void> {
    const workspace = this.items.find((item) => item.workspace_id === workspaceId);
    if (!workspace) return;
    const index = workspace.session_ids.indexOf(sessionId);
    const target = index + direction;
    if (index < 0 || target < 0 || target >= workspace.session_ids.length) return;
    const beforeSessionId = direction < 0
      ? workspace.session_ids[target]
      : workspace.session_ids[index + 2] || null;
    const updated = await this.api.reorderWorkspaceSession(
      workspaceId,
      sessionId,
      beforeSessionId,
    );
    this.revision += 1;
    this.upsert(updated);
  }

  handleCatalogEvent(event: ServerEvent): void {
    this.refreshEvents?.push(event);
    this.applyCatalogEvent(event);
    this.publish();
  }

  private applyCatalogEvent(event: ServerEvent): void {
    this.eventCursor = Math.max(this.eventCursor, event.sequence);
    if (event.type === "catalog/workspace-changed") {
      const workspace = workspaceFrame(event);
      this.removedIds.delete(workspace.workspace_id);
      this.upsert(workspace, false);
      return;
    }
    if (event.type === "catalog/workspace-removed") {
      const workspaceId = stringField(event.data, "workspace_id");
      this.removedIds.add(workspaceId);
      this.items = this.items.filter((workspace) => workspace.workspace_id !== workspaceId);
    } else if (event.type === "catalog/workspace-order-changed") {
      this.installOrder(stringArrayField(event.data, "workspace_ids"), false);
      return;
    } else if (event.type === "catalog/archived-sessions-changed") {
      this.archivedSessionIds = stringArrayField(event.data, "archived_session_ids");
    }
  }

  private upsert(workspace: WorkspaceData, publish = true): void {
    if (this.removedIds.has(workspace.workspace_id)) return;
    const index = this.items.findIndex((item) => item.workspace_id === workspace.workspace_id);
    if (index < 0) this.items = [workspace, ...this.items];
    else {
      const current = this.items[index];
      if (Date.parse(workspace.updated_at) < Date.parse(current.updated_at)) return;
      this.items = this.items.map((item, position) => position === index ? workspace : item);
    }
    if (publish) this.publish();
  }

  private installOrder(workspaceIds: readonly string[], publish = true): void {
    const rank = new Map(workspaceIds.map((workspaceId, index) => [workspaceId, index]));
    this.items = [...this.items].sort((left, right) => (
      (rank.get(left.workspace_id) ?? Number.MAX_SAFE_INTEGER)
      - (rank.get(right.workspace_id) ?? Number.MAX_SAFE_INTEGER)
    ));
    if (publish) this.publish();
  }

  private publish(): void {
    this.snapshot = this.buildSnapshot();
    for (const listener of this.listeners) listener();
  }

  private buildSnapshot(): WorkspaceSnapshot {
    return {
      items: this.items,
      archivedSessionIds: this.archivedSessionIds,
      eventCursor: this.eventCursor,
      state: this.state,
      error: this.error,
    };
  }
}

function workspaceFrame(event: ServerEvent): WorkspaceData {
  const value = event.data.workspace;
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new Error(`${event.type} has no workspace object`);
  }
  const workspace = value as Record<string, unknown>;
  stringField(workspace, "workspace_id");
  stringArrayField(workspace, "session_ids");
  return workspace as unknown as WorkspaceData;
}

function stringField(value: Record<string, unknown>, field: string): string {
  const result = value[field];
  if (typeof result !== "string" || !result) {
    throw new Error(`Workspace event field ${field} must be a non-empty string`);
  }
  return result;
}

function stringArrayField(value: Record<string, unknown>, field: string): string[] {
  const result = value[field];
  if (!Array.isArray(result) || result.some((item) => typeof item !== "string")) {
    throw new Error(`Workspace event field ${field} must be a string array`);
  }
  return result;
}
