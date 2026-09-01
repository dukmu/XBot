import { describe, expect, it, vi } from "vitest";
import type { OpenSessionResponse, ServerEvent } from "../api/types";
import { RuntimeEventController, type RuntimeEventListener } from "./RuntimeEventController";

describe("RuntimeEventController", () => {
  it("publishes high-frequency deltas as one timed batch", async () => {
    vi.useFakeTimers();
    const batches: ServerEvent[][] = [];
    const events = [event("assistant_message_delta"), event("tool_call_delta")];
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
});

function event(type: ServerEvent["type"]): ServerEvent {
  return { type, data: {} } as ServerEvent;
}

function listener(overrides: Partial<RuntimeEventListener>): RuntimeEventListener {
  return {
    onEvents: () => undefined,
    onThreads: () => undefined,
    onTaskExpired: () => undefined,
    onConnection: () => undefined,
    onError: () => undefined,
    ...overrides,
  };
}
