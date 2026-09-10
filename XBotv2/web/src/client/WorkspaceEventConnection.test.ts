import { describe, expect, it, vi } from "vitest";
import { XBotApiError } from "../api/client";
import type { ServerEvent } from "../api/types";
import { WorkspaceEventConnection, type WorkspaceEventListener } from "./WorkspaceEventConnection";

describe("WorkspaceEventConnection", () => {
  it("resumes from the last delivered sequence after a transport failure", async () => {
    const cursors: number[] = [];
    let calls = 0;
    const api = {
      async *streamWorkspaceEvents(after: number) {
        cursors.push(after);
        calls += 1;
        if (calls === 1) {
          yield frame(4);
          throw new TypeError("network lost");
        }
        yield frame(5);
        await new Promise(() => undefined);
      },
    };
    const received: number[] = [];
    const listener = listeners({ onEvent: (event) => received.push(event.sequence) });
    const connection = new WorkspaceEventConnection(api, async () => undefined);

    connection.start(3, listener);
    await vi.waitFor(() => expect(received).toEqual([4, 5]));

    expect(cursors).toEqual([3, 4]);
    expect(listener.onConnection).toHaveBeenCalledWith(true);
    connection.stop();
  });

  it("requests a new baseline instead of looping an expired cursor", async () => {
    const reset = vi.fn();
    const api = {
      async *streamWorkspaceEvents(): AsyncGenerator<ServerEvent> {
        throw new XBotApiError(409, "workspace_event_cursor_expired", "expired", true);
      },
    };
    const connection = new WorkspaceEventConnection(api, async () => undefined);
    connection.start(0, listeners({ onResetRequired: reset }));

    await vi.waitFor(() => expect(reset).toHaveBeenCalledOnce());
  });
});

function listeners(overrides: Partial<WorkspaceEventListener>): WorkspaceEventListener {
  return {
    onEvent: vi.fn(),
    onConnection: vi.fn(),
    onResetRequired: vi.fn(),
    onError: vi.fn(),
    ...overrides,
  };
}

function frame(sequence: number): ServerEvent {
  return {
    protocol_version: "xbotv2.v3",
    session_id: "",
    thread_id: "workspaces",
    request_id: "",
    sequence,
    type: "catalog/session-changed",
    data: {},
  };
}
