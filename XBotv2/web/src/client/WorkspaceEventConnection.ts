import { XBotApiError } from "../api/client";
import type { ServerEvent } from "../api/types";

export interface WorkspaceEventListener {
  onEvent(event: ServerEvent): void;
  onConnection(connected: boolean): void;
  onResetRequired(): void;
  onError(error: unknown): void;
}

export class WorkspaceEventConnection {
  private controller: AbortController | null = null;
  private generation = 0;

  constructor(
    private readonly api: { streamWorkspaceEvents(after: number, signal?: AbortSignal): AsyncIterable<ServerEvent> },
    private readonly retryDelay = defaultRetryDelay,
  ) {}

  start(after: number, listener: WorkspaceEventListener): void {
    this.stop();
    const generation = this.generation;
    const controller = new AbortController();
    this.controller = controller;
    void this.consume(after, listener, controller, generation);
  }

  stop(): void {
    this.generation += 1;
    this.controller?.abort();
    this.controller = null;
  }

  private async consume(
    cursor: number,
    listener: WorkspaceEventListener,
    controller: AbortController,
    generation: number,
  ): Promise<void> {
    let attempt = 0;
    while (this.isCurrent(controller, generation)) {
      let connected = false;
      try {
        for await (const event of this.api.streamWorkspaceEvents(cursor, controller.signal)) {
          if (!this.isCurrent(controller, generation)) return;
          if (!connected) {
            connected = true;
            listener.onConnection(true);
          }
          try {
            listener.onEvent(event);
          } catch (error) {
            listener.onConnection(false);
            listener.onError(error);
            return;
          }
          cursor = Math.max(cursor, event.sequence);
          attempt = 0;
        }
        if (!this.isCurrent(controller, generation)) return;
        throw new Error("Workspace event stream ended unexpectedly");
      } catch (error) {
        if (!this.isCurrent(controller, generation)) return;
        listener.onConnection(false);
        if (error instanceof XBotApiError && error.code === "workspace_event_cursor_expired") {
          this.stop();
          listener.onResetRequired();
          return;
        }
        if (error instanceof XBotApiError && !error.retryable) {
          listener.onError(error);
          return;
        }
      }
      attempt += 1;
      try {
        await this.retryDelay(Math.min(250 * 2 ** (attempt - 1), 4000), controller.signal);
      } catch {
        return;
      }
    }
  }

  private isCurrent(controller: AbortController, generation: number): boolean {
    return !controller.signal.aborted
      && controller === this.controller
      && generation === this.generation;
  }
}

function defaultRetryDelay(milliseconds: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    const timer = window.setTimeout(resolve, milliseconds);
    signal.addEventListener("abort", () => {
      window.clearTimeout(timer);
      reject(new DOMException("Aborted", "AbortError"));
    }, { once: true });
  });
}
