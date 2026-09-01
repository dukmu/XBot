import type { XBotApi } from "../api/client";
import type { OpenSessionResponse, ServerEvent, ThreadSummary } from "../api/types";
import { SessionEventConnection } from "./SessionEventConnection";

type SessionAddress = Pick<OpenSessionResponse, "session_id" | "thread_id" | "event_cursor">;

export interface RuntimeEventListener {
  onEvents(events: ServerEvent[]): void;
  onThreads(threads: ThreadSummary[]): void;
  onTaskExpired(taskId: string): void;
  onConnection(connected: boolean): void;
  onError(error: unknown): void;
}

export class RuntimeEventController {
  private readonly connection: SessionEventConnection;
  private events: ServerEvent[] = [];
  private flushTimer: number | null = null;
  private threadRefreshTimer: number | null = null;
  private readonly taskTimers = new Map<string, number>();
  private generation: number | null = null;

  constructor(
    private readonly api: Pick<XBotApi, "streamEvents" | "listThreads">,
    private readonly listener: RuntimeEventListener,
  ) {
    this.connection = new SessionEventConnection(api);
  }

  start(session: SessionAddress, generation: number): void {
    this.generation = generation;
    this.connection.start(session, {
      onEvent: (event) => this.handle(event, generation),
      onConnection: (connected) => {
        if (this.isCurrent(generation)) this.listener.onConnection(connected);
      },
      onDisconnect: (error, retrying) => {
        if (this.isCurrent(generation) && !retrying) this.listener.onError(error);
      },
    });
  }

  stop(): void {
    this.generation = null;
    this.connection.stop();
    if (this.flushTimer !== null) window.clearTimeout(this.flushTimer);
    if (this.threadRefreshTimer !== null) window.clearTimeout(this.threadRefreshTimer);
    for (const timer of this.taskTimers.values()) window.clearTimeout(timer);
    this.events = [];
    this.flushTimer = null;
    this.threadRefreshTimer = null;
    this.taskTimers.clear();
  }

  handle(event: ServerEvent, generation: number): void {
    if (!this.isCurrent(generation)) return;
    if (event.type === "assistant_message_delta" || event.type === "tool_call_delta") {
      this.events.push(event);
      if (this.flushTimer === null) {
        this.flushTimer = window.setTimeout(() => this.flush(generation), 16);
      }
    } else {
      this.flush(generation);
      this.listener.onEvents([event]);
    }
    if (event.type === "task_updated") this.handleTask(event, generation);
  }

  private flush(generation: number): void {
    if (this.flushTimer !== null) window.clearTimeout(this.flushTimer);
    this.flushTimer = null;
    if (!this.events.length) return;
    const events = this.events;
    this.events = [];
    if (this.isCurrent(generation)) this.listener.onEvents(events);
  }

  private handleTask(event: ServerEvent, generation: number): void {
    const taskId = String(event.data.task_id || "");
    const status = String(event.data.status || "");
    const existing = this.taskTimers.get(taskId);
    if (existing !== undefined) window.clearTimeout(existing);
    if (taskId && (status === "completed" || status === "stopped")) {
      this.taskTimers.set(taskId, window.setTimeout(() => {
        if (this.isCurrent(generation)) this.listener.onTaskExpired(taskId);
        this.taskTimers.delete(taskId);
      }, 4000));
    }
    if (event.data.kind !== "agent" || !event.session_id) return;
    if (this.threadRefreshTimer !== null) window.clearTimeout(this.threadRefreshTimer);
    this.threadRefreshTimer = window.setTimeout(() => {
      this.threadRefreshTimer = null;
      if (!this.isCurrent(generation)) return;
      void this.api.listThreads(event.session_id)
        .then((threads) => {
          if (this.isCurrent(generation)) this.listener.onThreads(threads);
        })
        .catch((error) => this.listener.onError(error));
    }, 250);
  }

  private isCurrent(generation: number): boolean {
    return generation === this.generation;
  }
}
