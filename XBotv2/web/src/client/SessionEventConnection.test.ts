import { describe, expect, it, vi } from "vitest";
import type { SessionView } from "../api/types";
import type { ServerEvent } from "../api/wire";
import { XBotApi, XBotApiError } from "../api/client";
import { SessionEventConnection, type SessionEventListener } from "./SessionEventConnection";

const session = {
  session_id: "session-1",
  thread_id: "agent",
  event_cursor: 4,
} as SessionView;

describe("SessionEventConnection", () => {
  it("marks an idle stream connected after the HTTP response opens", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(new ReadableStream(), { status: 200 }),
    );
    const listener: SessionEventListener = {
      onEvent: vi.fn(),
      onConnection: vi.fn(),
      onDisconnect: vi.fn(),
      onResetRequired: vi.fn(),
    };
    const connection = new SessionEventConnection(new XBotApi("http://xbot.test"));
    try {
      connection.start(session, listener);
      await vi.waitFor(() => expect(listener.onConnection).toHaveBeenCalledWith(true));
      expect(listener.onEvent).not.toHaveBeenCalled();
      expect(listener.onDisconnect).not.toHaveBeenCalled();
    } finally {
      connection.stop();
      fetchMock.mockRestore();
    }
  });

  it("reconnects after a transport failure and delivers the next stream", async () => {
    let calls = 0;
    const cursors: number[] = [];
    const event = { kind: "usage", payload: {}, sequence: 5 } as ServerEvent;
    const transport = {
      async *streamEvents(_sessionId: string, _threadId: string, after: number) {
        calls += 1;
        cursors.push(after);
        if (calls === 1) throw new TypeError("network lost");
        if (calls === 2) {
          yield event;
          throw new TypeError("network lost again");
        }
        await new Promise(() => undefined);
      },
    };
    const received: ServerEvent[] = [];
    const disconnected: boolean[] = [];
    const listener: SessionEventListener = {
      onEvent: (value) => received.push(value),
      onConnection: vi.fn(),
      onDisconnect: (_error, retrying) => disconnected.push(retrying),
      onResetRequired: vi.fn(),
    };
    const connection = new SessionEventConnection(transport, async () => undefined);

    connection.start(session, listener);
    await vi.waitFor(() => expect(received).toEqual([event]));
    await vi.waitFor(() => expect(cursors).toContain(5));

    expect(calls).toBe(3);
    expect(disconnected.every(Boolean)).toBe(true);
    connection.stop();
  });

  it("stops without retrying when the active connection is replaced", async () => {
    let aborted = false;
    const transport = {
      async *streamEvents(
        _sessionId: string,
        _threadId: string,
        _after: number,
        signal?: AbortSignal,
      ) {
        signal?.addEventListener("abort", () => { aborted = true; }, { once: true });
        await new Promise(() => undefined);
        yield {} as ServerEvent;
      },
    };
    const listener: SessionEventListener = {
      onEvent: vi.fn(),
      onConnection: vi.fn(),
      onDisconnect: vi.fn(),
      onResetRequired: vi.fn(),
    };
    const connection = new SessionEventConnection(transport, async () => undefined);

    connection.start(session, listener);
    await Promise.resolve();
    connection.stop();

    expect(aborted).toBe(true);
    expect(listener.onDisconnect).not.toHaveBeenCalled();
  });

  it("stops and reports cursor expiry instead of retrying forever", async () => {
    let calls = 0;
    const transport = {
      async *streamEvents() {
        calls += 1;
        throw new XBotApiError(
          409,
          "session_event_cursor_expired",
          "expired",
          true,
        );
        yield {} as ServerEvent;
      },
    };
    const disconnects: boolean[] = [];
    const reset = vi.fn();
    const listener: SessionEventListener = {
      onEvent: vi.fn(),
      onConnection: vi.fn(),
      onDisconnect: (_error, retrying) => disconnects.push(retrying),
      onResetRequired: reset,
    };
    const connection = new SessionEventConnection(transport, async () => undefined);

    connection.start(session, listener);
    await vi.waitFor(() => expect(disconnects).toEqual([false]));

    expect(calls).toBe(1);
    expect(reset).toHaveBeenCalledOnce();
    connection.stop();
  });

  it("requests a baseline when the replay stream skips a sequence", async () => {
    const reset = vi.fn();
    const disconnected: boolean[] = [];
    const transport = {
      async *streamEvents() {
        yield { ...({ kind: "usage", payload: {} } as ServerEvent), sequence: 6 };
      },
    };
    const listener: SessionEventListener = {
      onEvent: vi.fn(),
      onConnection: vi.fn(),
      onDisconnect: (_error, retrying) => disconnected.push(retrying),
      onResetRequired: reset,
    };
    const connection = new SessionEventConnection(transport, async () => undefined);

    connection.start(session, listener);
    await vi.waitFor(() => expect(reset).toHaveBeenCalledOnce());

    expect(listener.onEvent).not.toHaveBeenCalled();
    expect(disconnected).toEqual([false]);
    connection.stop();
  });
});
