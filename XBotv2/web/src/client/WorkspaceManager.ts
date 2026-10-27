import type { XBotApi } from "../api/client";
import type { ViewEvent, WorkspaceData } from "../api/types";

export interface WorkspaceSnapshot {
  items: readonly WorkspaceData[];
  canPrevious: boolean;
  canNext: boolean;
  eventCursor: number;
  state: "idle" | "loading" | "error";
  error: unknown;
}

type WorkspaceApi = Pick<
  XBotApi,
  "listWorkspaces" | "createWorkspace" | "renameWorkspace" | "deleteWorkspace"
  | "reorderWorkspace" | "reorderWorkspaceSession" | "setSessionArchived"
>;

export class WorkspaceManager {
  private items: readonly WorkspaceData[] = [];
  private cursor: string | null = null;
  private previousCursor: string | null = null;
  private nextCursor: string | null = null;
  private eventCursor = 0;
  private state: WorkspaceSnapshot["state"] = "idle";
  private error: unknown = null;
  private snapshot = this.buildSnapshot();
  private readonly listeners = new Set<() => void>();
  private refreshPromise: Promise<void> | null = null;
  private dirty = false;
  private generation = 0;

  constructor(private readonly api: WorkspaceApi) {}
  subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  };
  getSnapshot = (): WorkspaceSnapshot => this.snapshot;

  async next(): Promise<void> {
    if (!this.nextCursor || this.state === "loading") return;
    this.cursor = this.nextCursor;
    this.generation++;
    await this.refresh();
  }
  async back(): Promise<void> {
    if (!this.previousCursor || this.state === "loading") return;
    this.cursor = this.previousCursor;
    this.generation++;
    await this.refresh();
  }
  refresh(): Promise<void> {
    if (this.refreshPromise) {
      this.dirty = true;
      return this.refreshPromise;
    }
    this.state = "loading";
    this.error = null;
    this.publish();
    this.refreshPromise = (async () => {
      do {
        this.dirty = false;
        const generation = this.generation;
        const result = await this.api.listWorkspaces(this.cursor);
        if (generation !== this.generation) {
          this.dirty = true;
          continue;
        }
        this.items = result.items;
        this.nextCursor = result.next_cursor;
        this.previousCursor = result.previous_cursor;
        this.eventCursor = Math.max(this.eventCursor, result.event_cursor);
      } while (this.dirty);
      this.state = "idle";
    })().catch((error) => {
      this.state = "error";
      this.error = error;
      throw error;
    })
      .finally(() => {
        this.refreshPromise = null;
        this.publish();
        if (this.dirty) void this.refresh().catch(() => undefined);
      });
    return this.refreshPromise;
  }
  async create(path: string): Promise<WorkspaceData> {
    const workspace = await this.api.createWorkspace(path);
    await this.refresh();
    return workspace;
  }

  async rename(workspaceId: string, title: string): Promise<void> {
    await this.api.renameWorkspace(workspaceId, title);
    await this.refresh();
  }

  async delete(workspaceId: string): Promise<void> {
    await this.api.deleteWorkspace(workspaceId);
    await this.refresh();
  }

  async move(workspaceId: string, direction: -1 | 1): Promise<void> {
    const index = this.items.findIndex((item) => item.workspace_id === workspaceId);
    const target = index + direction;
    if (index < 0 || target < 0 || target >= this.items.length) return;
    const moving = direction < 0 ? workspaceId : this.items[target].workspace_id;
    const before = direction < 0 ? this.items[target].workspace_id : workspaceId;
    await this.api.reorderWorkspace(moving, before);
    await this.refresh();
  }
  async setSessionArchived(sessionId: string, archived: boolean): Promise<void> {
    await this.api.setSessionArchived(sessionId, archived);
  }

  async moveSession(
    workspaceId: string,
    sessionIds: readonly string[],
    sessionId: string,
    direction: -1 | 1,
  ): Promise<void> {
    const index = sessionIds.indexOf(sessionId);
    const target = index + direction;
    if (index < 0 || target < 0 || target >= sessionIds.length) return;
    const moving = direction < 0 ? sessionId : sessionIds[target];
    const before = direction < 0 ? sessionIds[target] : sessionId;
    await this.api.reorderWorkspaceSession(workspaceId, moving, before);
  }
  handleCatalogEvent(event: ViewEvent): void {
    this.eventCursor = Math.max(this.eventCursor, event.sequence);
    if (
      event.type === "catalog/workspace-changed"
      || event.type === "catalog/workspace-removed"
      || event.type === "catalog/workspace-order-changed"
    ) {
      void this.refresh().catch(() => undefined);
    }
  }
  private publish(): void {
    this.snapshot = this.buildSnapshot();
    for (const listener of this.listeners) listener();
  }
  private buildSnapshot(): WorkspaceSnapshot {
    return {
      items: this.items,
      canPrevious: this.previousCursor !== null,
      canNext: this.nextCursor !== null,
      eventCursor: this.eventCursor,
      state: this.state,
      error: this.error,
    };
  }
}
