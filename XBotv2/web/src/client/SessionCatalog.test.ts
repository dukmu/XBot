import { describe, expect, it } from "vitest";
import type { SessionSummary } from "../api/types";
import { SessionCatalog } from "./SessionCatalog";

const session: SessionSummary = {
  session_id: "session-1",
  status: "inactive",
  active_threads: 0,
  thread_count: 1,
  blank: false,
};

describe("SessionCatalog", () => {
  it("publishes one shared refresh and its result", async () => {
    let calls = 0;
    const catalog = new SessionCatalog({
      listSessions: async () => { calls += 1; return { sessions: [session], event_cursor: 3 }; },
      renameSession: async (_sessionId, title) => ({ ...session, title }),
    });

    await Promise.all([catalog.refresh(), catalog.refresh()]);

    expect(calls).toBe(1);
    expect(catalog.getSnapshot().eventCursor).toBe(3);
    await catalog.rename(session.session_id, "Renamed");
    expect(catalog.getSnapshot().items[0]?.title).toBe("Renamed");
  });

  it("does not let an older baseline resurrect a removed session", async () => {
    let resolveList: (items: SessionSummary[]) => void = () => undefined;
    const list = new Promise<SessionSummary[]>((resolve) => { resolveList = resolve; });
    const catalog = new SessionCatalog({
      listSessions: async () => ({ sessions: await list, event_cursor: 0 }),
      renameSession: async (_sessionId, title) => ({ ...session, title }),
    });

    const refresh = catalog.refresh();
    catalog.remove(session.session_id);
    resolveList([session]);
    await refresh;

    expect(catalog.getSnapshot().items).toEqual([]);
  });

  it("does not let an in-flight baseline overwrite a newer Workspace frame", async () => {
    let resolveList: (items: SessionSummary[]) => void = () => undefined;
    const list = new Promise<SessionSummary[]>((resolve) => { resolveList = resolve; });
    const catalog = new SessionCatalog({
      listSessions: async () => ({ sessions: await list, event_cursor: 2 }),
      renameSession: async (_sessionId, title) => ({ ...session, title }),
    });

    const refresh = catalog.refresh();
    catalog.handleCatalogEvent({
      protocol_version: "xbotv2.v3", session_id: "", thread_id: "workspaces",
      request_id: "", sequence: 3, type: "catalog/session-changed",
      data: { session: { ...session, title: "From frame" } },
    });
    resolveList([{ ...session, title: "Stale baseline" }]);
    await refresh;

    expect(catalog.getSnapshot().items[0]?.title).toBe("From frame");
    expect(catalog.getSnapshot().eventCursor).toBe(3);
  });

  it("installs the complete baseline before replaying frames received during refresh", async () => {
    let resolveList: (items: SessionSummary[]) => void = () => undefined;
    const list = new Promise<SessionSummary[]>((resolve) => { resolveList = resolve; });
    const other = { ...session, session_id: "session-2" };
    const catalog = new SessionCatalog({
      listSessions: async () => ({ sessions: await list, event_cursor: 2 }),
      renameSession: async (_sessionId, title) => ({ ...session, title }),
    });

    const refresh = catalog.refresh();
    catalog.handleCatalogEvent({
      protocol_version: "xbotv2.v3", session_id: "", thread_id: "workspaces",
      request_id: "", sequence: 3, type: "catalog/session-changed",
      data: { session: { ...session, title: "From frame" } },
    });
    resolveList([{ ...session, title: "Stale baseline" }, other]);
    await refresh;

    expect(catalog.getSnapshot().items.map((item) => item.title)).toEqual(["From frame", undefined]);
  });
});
