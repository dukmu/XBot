import { afterEach, expect, it, vi } from "vitest";
import { XBotApi } from "./client";

afterEach(() => vi.unstubAllGlobals());

it("uses the production envelopes, page cursors and command request", async () => {
  const responses = [
    { data: { key: { session_id: "s", thread_id: "main" }, history: { items: [], older_cursor: "old" } } },
    { items: [{ kind: "human_input", id: "u", content: "hello", media: [], artifacts: [] }], older_cursor: null },
    { page: { items: [], older_cursor: "older" }, newest_position: 8 },
    { removed_turns: 1, history: { items: [], older_cursor: null }, stats: {} },
    { type: "command_result", data: { command: "status", status: "ok", message: "ok", effects: [] } },
  ];
  const fetcher = vi.fn().mockImplementation(() => Promise.resolve(new Response(JSON.stringify(responses.shift()), { status: 200 })));
  vi.stubGlobal("fetch", fetcher);
  const api = new XBotApi();
  expect((await api.openSession({ mode: "new" })).key).toEqual({ session_id: "s", thread_id: "main" });
  expect((await api.listMessages("s", "main", { cursor: "old" })).items[0].kind).toBe("human_input");
  expect((await api.listTrajectory("s", "main")).page.older_cursor).toBe("older");
  expect((await api.undoHistory("s", "main")).removed_turns).toBe(1);
  await api.runCommand("s", "main", "status", "/status");
  expect(JSON.parse(fetcher.mock.calls[4][1].body)).toEqual({ raw: "/status" });
});
