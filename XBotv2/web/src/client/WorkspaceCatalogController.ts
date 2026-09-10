import type { XBotApi } from "../api/client";
import type { ServerEvent } from "../api/types";
import type { SessionSummary } from "../api/types";
import { WorkspaceEventConnection } from "./WorkspaceEventConnection";
import type { SessionCatalog } from "./SessionCatalog";
import type { WorkspaceManager } from "./WorkspaceManager";

export interface WorkspaceCatalogListener {
  onConnection(connected: boolean): void;
  onError(error: unknown): void;
}

export class WorkspaceCatalogController {
  private readonly connection: WorkspaceEventConnection;
  private running = false;
  private baselineGeneration = 0;

  constructor(
    api: Pick<XBotApi, "streamWorkspaceEvents">,
    private readonly sessions: SessionCatalog,
    private readonly workspaces: WorkspaceManager,
    private readonly listener: WorkspaceCatalogListener,
  ) {
    this.connection = new WorkspaceEventConnection(api);
  }

  async start(): Promise<void> {
    this.running = true;
    await this.resetBaseline();
  }

  stop(): void {
    this.running = false;
    this.baselineGeneration += 1;
    this.connection.stop();
  }

  refresh(): Promise<void> {
    return this.refreshBaselines();
  }

  reusableSession(workspaceRoot: string): SessionSummary | undefined {
    if (!workspaceRoot) return undefined;
    const workspaces = this.workspaces.getSnapshot();
    const workspace = workspaces.items.find((item) => item.path === workspaceRoot);
    if (!workspace) return undefined;
    const archived = new Set(workspaces.archivedSessionIds);
    const sessions = new Map(
      this.sessions.getSnapshot().items.map((session) => [session.session_id, session]),
    );
    for (const sessionId of workspace.session_ids) {
      const session = sessions.get(sessionId);
      if (
        session?.blank
        && session.workspace_root === workspaceRoot
        && !archived.has(sessionId)
      ) return session;
    }
    return undefined;
  }

  private async resetBaseline(): Promise<void> {
    const generation = ++this.baselineGeneration;
    try {
      await this.refreshBaselines();
      if (!this.running || generation !== this.baselineGeneration) return;
      const cursor = Math.min(
        this.sessions.getSnapshot().eventCursor,
        this.workspaces.getSnapshot().eventCursor,
      );
      this.connection.start(cursor, {
        onEvent: (event) => this.handle(event),
        onConnection: this.listener.onConnection,
        onResetRequired: () => void this.resetBaseline(),
        onError: this.listener.onError,
      });
    } catch (error) {
      if (this.running && generation === this.baselineGeneration) {
        this.listener.onError(error);
      }
    }
  }

  private async refreshBaselines(): Promise<void> {
    await Promise.all([this.sessions.refresh(), this.workspaces.refresh()]);
  }

  private handle(event: ServerEvent): void {
    this.sessions.handleCatalogEvent(event);
    this.workspaces.handleCatalogEvent(event);
  }
}
