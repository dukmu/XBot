import { describe, expect, it, vi } from "vitest";
import { SessionCatalog } from "./SessionCatalog";
import type { SessionSummary, ViewEvent } from "../api/types";

const item = (id: string): SessionSummary => ({ session_id: id, title: id, status: "inactive", active_threads: 0, thread_count: 1, workspace_root: "/w", blank: false });
const page = (ids: string[], previous_cursor: string | null, next_cursor: string | null) => ({ sessions: ids.map(item), previous_cursor, next_cursor, event_cursor: 4 });
const event = (type: string): ViewEvent => ({ protocol_version: "xbotv2.v3", session_id: "", thread_id: "", request_id: "", sequence: 5, type, data: {} });

describe("bounded session catalog", () => {
  it("replaces each window and follows server cursors in both directions", async () => {
    const listSessions = vi.fn(async (cursor?: string | null) => cursor === "next" ? page(["b"], "previous", null) : page(["a"], null, "next"));
    const api = { listSessions, listWorkspaceSessions: vi.fn(), listArchivedSessions: vi.fn(), renameSession: vi.fn() };
    const catalog = new SessionCatalog(api);
    await catalog.refresh();
    await catalog.next();
    expect(catalog.getSnapshot()).toMatchObject({ items: [item("b")], canPrevious: true, canNext: false });
    await catalog.back();
    expect(catalog.getSnapshot()).toMatchObject({ items: [item("a")], canPrevious: false, canNext: true });
    expect(listSessions.mock.calls.map(([cursor]) => cursor)).toEqual([null, "next", "previous"]);
  });

  it("replaces the selected scope and refreshes it on a relevant change", async () => {
    const listWorkspaceSessions = vi.fn(async () => page(["w1", "w2"], null, null));
    const api = { listSessions: vi.fn(async () => page(["all"], null, null)), listWorkspaceSessions, listArchivedSessions: vi.fn(async () => page(["archived"], null, null)), renameSession: vi.fn() };
    const catalog = new SessionCatalog(api);
    await catalog.refresh();
    await catalog.select({ kind: "workspace", workspaceId: "w" });
    expect(catalog.getSnapshot()).toMatchObject({ items: [item("w1"), item("w2")], archivedIds: [] });
    catalog.handleCatalogEvent(event("catalog/workspace-session-order-changed"));
    await vi.waitFor(() => expect(listWorkspaceSessions).toHaveBeenCalledTimes(2));
    await catalog.select({ kind: "archived" });
    expect(catalog.getSnapshot()).toMatchObject({ items: [item("archived")], archivedIds: ["archived"] });
  });

  it("discards a late page when the scope changes during a request", async () => {
    let resolve!: (value: ReturnType<typeof page>) => void;
    const api = { listSessions: vi.fn(() => new Promise<ReturnType<typeof page>>((done) => { resolve = done; })), listWorkspaceSessions: vi.fn(), listArchivedSessions: vi.fn(async () => page(["archived"], null, null)), renameSession: vi.fn() };
    const catalog = new SessionCatalog(api);
    const first = catalog.refresh();
    const selected = catalog.select({ kind: "archived" });
    resolve(page(["stale"], null, null));
    await Promise.all([first, selected]);
    expect(catalog.getSnapshot()).toMatchObject({ items: [item("archived")], scope: { kind: "archived" } });
  });

  it("re-fetches a page when a change arrives during refresh", async () => {
    let resolve!: (value: ReturnType<typeof page>) => void;
    const listSessions = vi.fn()
      .mockImplementationOnce(() => new Promise((done) => { resolve = done; }))
      .mockResolvedValue(page(["new"], null, null));
    const catalog = new SessionCatalog({ listSessions, listWorkspaceSessions: vi.fn(), listArchivedSessions: vi.fn(), renameSession: vi.fn() });
    const refresh = catalog.refresh();
    catalog.handleCatalogEvent(event("catalog/session-removed"));
    resolve(page(["removed"], null, null));
    await refresh;
    expect(catalog.getSnapshot().items).toEqual([item("new")]);
    expect(listSessions).toHaveBeenCalledTimes(2);
  });

  it("retains the server's previous cursor for an emptied last page", async () => {
    const listSessions = vi.fn(async (cursor?: string | null) => cursor === "next"
      ? page([], "prev", null)
      : page(["first"], null, "next"));
    const catalog = new SessionCatalog({ listSessions, listWorkspaceSessions: vi.fn(), listArchivedSessions: vi.fn(), renameSession: vi.fn() });
    await catalog.refresh();
    await catalog.next();
    expect(catalog.getSnapshot()).toMatchObject({ items: [], canPrevious: true });
    await catalog.back();
    expect(catalog.getSnapshot().items).toEqual([item("first")]);
  });


  it("pages workspace members without retaining the previous window", async () => {
    const listWorkspaceSessions = vi.fn(async (_workspace: string, cursor?: string | null) => (
      cursor === "member-next" ? page(["member-101"], "member-prev", null) : page(["member-1"], null, "member-next")
    ));
    const catalog = new SessionCatalog({ listSessions: vi.fn(), listWorkspaceSessions, listArchivedSessions: vi.fn(), renameSession: vi.fn() });
    await catalog.select({ kind: "workspace", workspaceId: "w" });
    await catalog.next();
    expect(catalog.getSnapshot()).toMatchObject({ items: [item("member-101")], canPrevious: true, canNext: false });
    await catalog.back();
    expect(catalog.getSnapshot().items).toEqual([item("member-1")]);
    expect(listWorkspaceSessions.mock.calls.map(([, cursor]) => cursor)).toEqual([null, "member-next", "member-prev"]);
  });

});
