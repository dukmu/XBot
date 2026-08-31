import { describe, expect, it, vi } from "vitest";
import type { OpenSessionResponse, ServerEvent } from "../api/types";
import { SessionEventConnection, type SessionEventListener } from "./SessionEventConnection";

const session = {
  session_id: "session-1",
  thread_id: "agent",
} as OpenSessionResponse;

describe("SessionEventConnection", () => {
  it("reconnects after a transport failure and delivers the next stream", async () => {
    let calls = 0;
    const event = { type: "usage", data: {} } as ServerEvent;
    const transport = {
      async *streamEvents() {
        calls += 1;
        if (calls === 1) throw new TypeError("network lost");
        yield event;
        await new Promise(() => undefined);
      },
    };
    const received: ServerEvent[] = [];
    const disconnected: boolean[] = [];
    const listener: SessionEventListener = {
      onEvent: (value) => received.push(value),
      onConnection: vi.fn(),
      onDisconnect: (_error, retrying) => disconnected.push(retrying),
    };
    const connection = new SessionEventConnection(transport, async () => undefined);

    connection.start(session, listener);
    await vi.waitFor(() => expect(received).toEqual([event]));

    expect(calls).toBe(2);
    expect(disconnected).toEqual([true]);
    connection.stop();
  });

  it("stops without retrying when the active connection is replaced", async () => {
    let aborted = false;
    const transport = {
      async *streamEvents(_sessionId: string, _threadId: string, signal?: AbortSignal) {
        signal?.addEventListener("abort", () => { aborted = true; }, { once: true });
        await new Promise(() => undefined);
        yield {} as ServerEvent;
      },
    };
    const listener: SessionEventListener = {
      onEvent: vi.fn(),
      onConnection: vi.fn(),
      onDisconnect: vi.fn(),
    };
    const connection = new SessionEventConnection(transport, async () => undefined);

    connection.start(session, listener);
    await Promise.resolve();
    connection.stop();

    expect(aborted).toBe(true);
    expect(listener.onDisconnect).not.toHaveBeenCalled();
  });
});
