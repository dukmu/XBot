import { describe, expect, it, vi } from "vitest";
import type { SessionView, ViewEvent } from "../api/types";
import { RuntimeEventController, type RuntimeEventListener } from "./RuntimeEventController";

describe("RuntimeEventController", () => {
  it("publishes high-frequency deltas as one timed batch", async () => {
    vi.useFakeTimers();
    const batches: ViewEvent[][] = [];
    const events = [event("assistant_message_delta", 1), event("tool_call_delta", 2)];
    const api = {
      async *streamEvents() {
        yield wire("assistant_text_delta", 1, { text: "" });
        yield wire("tool_call_delta", 2, { call_id: "", name_delta: "", arguments_delta: "" });
        await new Promise(() => undefined);
      },
      artifactUrl: () => "",
      listThreads: async () => [],
    };
    const controller = new RuntimeEventController(api, listener({ onEvents: (batch) => batches.push(batch) }));

    controller.start({ session_id: "s", thread_id: "t", event_cursor: 0 } as SessionView, 1);
    await vi.advanceTimersByTimeAsync(0);
    expect(vi.getTimerCount()).toBeGreaterThan(0);
    await vi.advanceTimersByTimeAsync(16);

    expect(batches[0].map((item) => item.type)).toEqual(events.map((item) => item.type));
    controller.stop();
    vi.useRealTimers();
  });

  it("refreshes an already-running thread without waiting for another event", async () => {
    vi.useFakeTimers();
    const listThreads = vi.fn(async () => []);
    const api = {
      async *streamEvents() {
        await Promise.resolve();
        yield wire("assistant_text_delta", 1, { text: "" });
        await new Promise(() => undefined);
      },
      artifactUrl: () => "",
      listThreads,
    };
    const controller = new RuntimeEventController(api, listener({}));

    controller.start({ session_id: "s", thread_id: "t", event_cursor: 0 } as SessionView, 1, true);
    await vi.advanceTimersByTimeAsync(749);
    expect(listThreads).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(1);
    expect(listThreads).toHaveBeenCalledWith("s");

    controller.stop();
    vi.useRealTimers();
  });


});

function event(type: ViewEvent["type"], sequence = 1): ViewEvent {
  return { type, data: {}, sequence } as ViewEvent;
}

function listener(overrides: Partial<RuntimeEventListener>): RuntimeEventListener {
  return {
    onEvents: () => undefined,
    onThreads: () => undefined,
    onJobExpired: () => undefined,
    onConnection: () => undefined,
    onError: () => undefined,
    onResetRequired: () => undefined,
    ...overrides,
  };
}

function wire(kind: string, sequence: number, payload: Record<string, unknown>): import("../api/wire").ServerEvent {
  return { protocol_version: "xbotv2.v3", session_id: "s", thread_id: "t", sequence, scope: { kind: "session" }, kind, payload };
}
