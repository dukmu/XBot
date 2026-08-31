import { XBotApi, XBotApiError } from "../api/client";
import type { OpenSessionResponse, ServerEvent } from "../api/types";

type SessionAddress = Pick<OpenSessionResponse, "session_id" | "thread_id">;

export interface SessionEventListener {
  onEvent(event: ServerEvent): void;
  onConnection(connected: boolean): void;
  onDisconnect(error: unknown, retrying: boolean): void;
}

export class SessionEventConnection {
  private controller: AbortController | null = null;
  private generation = 0;

  constructor(
    private readonly api: Pick<XBotApi, "streamEvents">,
    private readonly retryDelay = defaultRetryDelay,
  ) {}

  start(session: SessionAddress, listener: SessionEventListener): void {
    this.stop();
    const generation = this.generation;
    const controller = new AbortController();
    this.controller = controller;
    void this.consume(session, listener, controller, generation);
  }

  stop(): void {
    this.generation += 1;
    this.controller?.abort();
    this.controller = null;
  }

  private async consume(
    session: SessionAddress,
    listener: SessionEventListener,
    controller: AbortController,
    generation: number,
  ): Promise<void> {
    let attempt = 0;
    while (this.isCurrent(controller, generation)) {
      listener.onConnection(true);
      try {
        for await (const event of this.api.streamEvents(
          session.session_id,
          session.thread_id,
          controller.signal,
        )) {
          if (!this.isCurrent(controller, generation)) return;
          attempt = 0;
          listener.onEvent(event);
        }
        if (!this.isCurrent(controller, generation)) return;
        listener.onConnection(false);
        listener.onDisconnect(new Error("Session event stream ended unexpectedly"), true);
      } catch (error) {
        if (!this.isCurrent(controller, generation)) return;
        const retrying = isRetryable(error);
        listener.onConnection(false);
        listener.onDisconnect(error, retrying);
        if (!retrying) return;
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

function isRetryable(error: unknown): boolean {
  return !(error instanceof XBotApiError) || error.retryable;
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
