import type { XBotApi } from "../api/client";
import type { ServerEvent, SessionSummary } from "../api/types";

export interface SessionCatalogSnapshot {
  items: readonly SessionSummary[];
  eventCursor: number;
  state: "idle" | "loading" | "error";
  error: unknown;
}

export class SessionCatalog {
  private items: readonly SessionSummary[] = [];
  private eventCursor = 0;
  private state: SessionCatalogSnapshot["state"] = "idle";
  private error: unknown = null;
  private snapshot = this.buildSnapshot();
  private readonly listeners = new Set<() => void>();
  private refreshPromise: Promise<void> | null = null;
  private refreshEvents: ServerEvent[] | null = null;
  private revision = 0;

  constructor(private readonly api: Pick<XBotApi, "listSessions" | "renameSession">) {}

  subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  };

  getSnapshot = (): SessionCatalogSnapshot => this.snapshot;

  refresh(): Promise<void> {
    if (this.refreshPromise) return this.refreshPromise;
    const revision = this.revision;
    const refreshEvents: ServerEvent[] = [];
    this.refreshEvents = refreshEvents;
    this.state = "loading";
    this.error = null;
    this.publish();
    this.refreshPromise = this.api.listSessions()
      .then((listing) => {
        if (revision === this.revision) {
          this.items = listing.sessions;
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

  remove(sessionId: string): void {
    this.revision += 1;
    this.items = this.items.filter((session) => session.session_id !== sessionId);
    this.publish();
  }

  async rename(sessionId: string, title: string): Promise<void> {
    const session = await this.api.renameSession(sessionId, title);
    this.revision += 1;
    const index = this.items.findIndex((item) => item.session_id === sessionId);
    this.items = index < 0
      ? [session, ...this.items]
      : this.items.map((item, position) => position === index ? session : item);
    this.publish();
  }

  handleCatalogEvent(event: ServerEvent): void {
    this.refreshEvents?.push(event);
    this.applyCatalogEvent(event);
    this.publish();
  }

  private applyCatalogEvent(event: ServerEvent): void {
    this.eventCursor = Math.max(this.eventCursor, event.sequence);
    if (event.type === "catalog/session-added" || event.type === "catalog/session-changed") {
      this.upsert(sessionFrame(event), false);
      return;
    }
    if (event.type === "catalog/session-removed") {
      const sessionId = stringField(event.data, "session_id");
      this.items = this.items.filter((session) => session.session_id !== sessionId);
    }
  }

  private upsert(session: SessionSummary, publish = true): void {
    const index = this.items.findIndex((item) => item.session_id === session.session_id);
    this.items = index < 0
      ? [session, ...this.items]
      : this.items.map((item, position) => position === index ? session : item);
    if (publish) this.publish();
  }

  private publish(): void {
    this.snapshot = this.buildSnapshot();
    for (const listener of this.listeners) listener();
  }

  private buildSnapshot(): SessionCatalogSnapshot {
    return {
      items: this.items,
      eventCursor: this.eventCursor,
      state: this.state,
      error: this.error,
    };
  }
}

function sessionFrame(event: ServerEvent): SessionSummary {
  const value = event.data.session;
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new Error(`${event.type} has no session object`);
  }
  const session = value as Record<string, unknown>;
  stringField(session, "session_id");
  const status = stringField(session, "status");
  if (status !== "active" && status !== "inactive") {
    throw new Error(`${event.type} has invalid session status`);
  }
  if (typeof session.blank !== "boolean") {
    throw new Error(`${event.type} has invalid blank state`);
  }
  return session as unknown as SessionSummary;
}

function stringField(value: Record<string, unknown>, field: string): string {
  const result = value[field];
  if (typeof result !== "string" || !result) {
    throw new Error(`Workspace event field ${field} must be a non-empty string`);
  }
  return result;
}
