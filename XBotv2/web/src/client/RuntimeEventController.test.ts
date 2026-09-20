import { describe, expect, it, vi } from "vitest";
import type { OpenSessionResponse, ServerEvent } from "../api/types";
import { RuntimeEventController, type RuntimeEventListener } from "./RuntimeEventController";

describe("RuntimeEventController", () => {
  it("publishes high-frequency deltas as one timed batch", async () => {
    vi.useFakeTimers();
    const batches: ServerEvent[][] = [];
    const events = [event("assistant_message_delta", 1), event("tool_call_delta", 2)];
    const api = {
      async *streamEvents() {
        yield events[0];
        yield events[1];
        await new Promise(() => undefined);
      },
      listThreads: async () => [],
    };
    const controller = new RuntimeEventController(api, listener({ onEvents: (batch) => batches.push(batch) }));

    controller.start({ session_id: "s", thread_id: "t", event_cursor: 0 } as OpenSessionResponse, 1);
    await vi.advanceTimersByTimeAsync(0);
    expect(vi.getTimerCount()).toBeGreaterThan(0);
    await vi.advanceTimersByTimeAsync(16);

    expect(batches).toEqual([events]);
    controller.stop();
    vi.useRealTimers();
  });

  it("refreshes an already-running thread without waiting for another event", async () => {
    vi.useFakeTimers();
    const listThreads = vi.fn(async () => []);
    const api = {
      async *streamEvents() {
        await Promise.resolve();
        yield event("assistant_message_delta");
        await new Promise(() => undefined);
      },
      listThreads,
    };
    const controller = new RuntimeEventController(api, listener({}));

    controller.start({ session_id: "s", thread_id: "t", event_cursor: 0 } as OpenSessionResponse, 1, true);
    await vi.advanceTimersByTimeAsync(749);
    expect(listThreads).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(1);
    expect(listThreads).toHaveBeenCalledWith("s");

    controller.stop();
    vi.useRealTimers();
  });

  it("refreshes thread summaries when a subagent job changes", async () => {
    vi.useFakeTimers();
    const listThreads = vi.fn(async () => []);
    const api = { async *streamEvents() { await new Promise(() => undefined); }, listThreads };
    const controller = new RuntimeEventController(api, listener({}));

    controller.start({ session_id: "s", thread_id: "t", event_cursor: 0 } as OpenSessionResponse, 1);
    controller.handle({
      ...event("job_updated"),
      session_id: "s",
      data: { kind: "subagent", thread_id: "child-1" },
    }, 1);
    await vi.advanceTimersByTimeAsync(250);
    expect(listThreads).toHaveBeenCalledWith("s");
    controller.stop();
    vi.useRealTimers();
  });
});

function event(type: ServerEvent["type"], sequence = 1): ServerEvent {
  return { type, data: {}, sequence } as ServerEvent;
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
