import { projectEvent } from "../state/wireProjection";
import type { XBotApi } from "../api/client";
import type { ViewEvent } from "../api/types";
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
        onEvent: (event) => this.handle(projectEvent(event, () => "")),
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

  private handle(event: ViewEvent): void {
    this.sessions.handleCatalogEvent(event);
    this.workspaces.handleCatalogEvent(event);
  }
}
