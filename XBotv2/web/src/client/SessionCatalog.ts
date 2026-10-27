import type { XBotApi } from "../api/client";
import type { ViewEvent, SessionSummary } from "../api/types";

export type SessionScope =
  | { kind: "all" }
  | { kind: "archived" }
  | { kind: "workspace"; workspaceId: string };
export interface SessionCatalogSnapshot {
  items: readonly SessionSummary[];
  archivedIds: readonly string[];
  scope: SessionScope;
  canPrevious: boolean;
  canNext: boolean;
  eventCursor: number;
  state: "idle" | "loading" | "error";
  error: unknown;
}

type CatalogApi = Pick<
  XBotApi,
  "listSessions" | "listWorkspaceSessions" | "listArchivedSessions" | "renameSession"
>;

export class SessionCatalog {
  private items: readonly SessionSummary[] = [];
  private archivedIds: readonly string[] = [];
  private scope: SessionScope = { kind: "all" };
  private cursor: string | null = null;
  private previousCursor: string | null = null;
  private nextCursor: string | null = null;
  private eventCursor = 0;
  private state: SessionCatalogSnapshot["state"] = "idle";
  private error: unknown = null;
  private snapshot = this.buildSnapshot();
  private readonly listeners = new Set<() => void>();
  private generation = 0;
  private appliedGeneration = -1;
  private refreshPromise: Promise<void> | null = null;
  private dirty = false;

  constructor(private readonly api: CatalogApi) {}
  subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  };
  getSnapshot = (): SessionCatalogSnapshot => this.snapshot;

  async select(scope: SessionScope): Promise<void> {
    this.scope = scope;
    this.cursor = null;
    this.previousCursor = null;
    this.nextCursor = null;
    this.items = [];
    this.archivedIds = [];
    this.generation++;
    await this.refresh();
    if (this.appliedGeneration !== this.generation) await this.refresh();
  }
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
        const scope = this.scope;
        const result = scope.kind === "workspace"
          ? await this.api.listWorkspaceSessions(scope.workspaceId, this.cursor)
          : scope.kind === "archived"
            ? await this.api.listArchivedSessions(this.cursor)
            : await this.api.listSessions(this.cursor);
        if (generation !== this.generation) {
          this.dirty = true;
          continue;
        }
        this.appliedGeneration = generation;
        this.items = result.sessions;
        this.archivedIds = scope.kind === "archived" ? result.sessions.map((item) => item.session_id) : [];
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
  async remove(_sessionId: string): Promise<void> {
    await this.refresh();
  }

  async rename(sessionId: string, title: string): Promise<void> {
    await this.api.renameSession(sessionId, title);
    await this.refresh();
  }

  handleCatalogEvent(event: ViewEvent): void {
    this.eventCursor = Math.max(this.eventCursor, event.sequence);
    if (
      event.type === "catalog/workspace-removed"
      && this.scope.kind === "workspace"
      && event.data.workspace_id === this.scope.workspaceId
    ) {
      void this.select({ kind: "all" }).catch(() => undefined);
      return;
    }
    if (
      event.type.startsWith("catalog/session-")
      || event.type === "catalog/workspace-session-order-changed"
      || event.type === "catalog/archived-sessions-changed"
    ) {
      void this.refresh().catch(() => undefined);
    }
  }
  private publish(): void {
    this.snapshot = this.buildSnapshot();
    for (const listener of this.listeners) listener();
  }
  private buildSnapshot(): SessionCatalogSnapshot {
    return {
      items: this.items,
      archivedIds: this.archivedIds,
      scope: this.scope,
      canPrevious: this.previousCursor !== null,
      canNext: this.nextCursor !== null,
      eventCursor: this.eventCursor,
      state: this.state,
      error: this.error,
    };
  }
}
