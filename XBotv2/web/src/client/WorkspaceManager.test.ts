import { describe, expect, it } from "vitest";
import type { WorkspaceData } from "../api/types";
import { WorkspaceManager } from "./WorkspaceManager";

const first = workspace("ws-1", "First", "2026-01-01T00:00:00Z");
const second = workspace("ws-2", "Second", "2026-01-01T00:00:00Z");

describe("WorkspaceManager", () => {
  it("publishes the baseline and unary changes without a second list request", async () => {
    let listCalls = 0;
    const api = fakeApi({
      listWorkspaces: async () => { listCalls += 1; return { items: [first, second], archived_session_ids: [], event_cursor: 4 }; },
      renameWorkspace: async () => ({ ...first, title: "Renamed", updated_at: "2026-01-02T00:00:00Z" }),
    });
    const manager = new WorkspaceManager(api);

    await manager.refresh();
    await manager.rename("ws-1", "Renamed");

    expect(manager.getSnapshot().items.map((item) => item.title)).toEqual(["Renamed", "Second"]);
    expect(listCalls).toBe(1);
    expect(manager.getSnapshot().eventCursor).toBe(4);
  });

  it("does not let an older refresh resurrect a deleted workspace", async () => {
    let resolveList: (items: WorkspaceData[]) => void = () => undefined;
    const list = new Promise<WorkspaceData[]>((resolve) => { resolveList = resolve; });
    const manager = new WorkspaceManager(fakeApi({
      listWorkspaces: async () => ({ items: await list, archived_session_ids: [], event_cursor: 0 }),
    }));

    const refresh = manager.refresh();
    await manager.delete("ws-1");
    resolveList([first]);
    await refresh;

    expect(manager.getSnapshot().items).toEqual([]);
  });

  it("optimistically reorders and rolls back a failed request", async () => {
    const api = fakeApi({ reorderWorkspace: async () => { throw new Error("reorder failed"); } });
    const manager = new WorkspaceManager(api);
    await manager.refresh();

    await expect(manager.move("ws-1", 1)).rejects.toThrow("reorder failed");

    expect(manager.getSnapshot().items.map((item) => item.workspace_id)).toEqual(["ws-1", "ws-2"]);
  });

  it("installs the authoritative session order returned by the server", async () => {
    const ordered = { ...first, session_ids: ["s2", "s1"] };
    const api = fakeApi({
      listWorkspaces: async () => ({
        items: [{ ...first, session_ids: ["s1", "s2"] }],
        archived_session_ids: [],
        event_cursor: 0,
      }),
      reorderWorkspaceSession: async () => ordered,
    });
    const manager = new WorkspaceManager(api);
    await manager.refresh();

    await manager.moveSession("ws-1", "s1", 1);

    expect(manager.getSnapshot().items[0].session_ids).toEqual(["s2", "s1"]);
  });

  it("protects a Workspace frame from an older in-flight baseline", async () => {
    let resolveList: (items: WorkspaceData[]) => void = () => undefined;
    const list = new Promise<WorkspaceData[]>((resolve) => { resolveList = resolve; });
    const manager = new WorkspaceManager(fakeApi({
      listWorkspaces: async () => ({ items: await list, archived_session_ids: [], event_cursor: 1 }),
    }));

    const refresh = manager.refresh();
    manager.handleCatalogEvent({
      protocol_version: "xbotv2.v3", session_id: "", thread_id: "workspaces",
      request_id: "", sequence: 2, type: "catalog/workspace-changed",
      data: { workspace: { ...first, title: "From frame", updated_at: "2026-01-02T00:00:00Z" } },
    });
    resolveList([{ ...first, title: "Stale baseline" }]);
    await refresh;

    expect(manager.getSnapshot().items[0].title).toBe("From frame");
    expect(manager.getSnapshot().eventCursor).toBe(2);
  });

  it("keeps untouched baseline workspaces while replaying an in-flight Workspace frame", async () => {
    let resolveList: (items: WorkspaceData[]) => void = () => undefined;
    const list = new Promise<WorkspaceData[]>((resolve) => { resolveList = resolve; });
    const manager = new WorkspaceManager(fakeApi({
      listWorkspaces: async () => ({ items: await list, archived_session_ids: [], event_cursor: 1 }),
    }));

    const refresh = manager.refresh();
    manager.handleCatalogEvent({
      protocol_version: "xbotv2.v3", session_id: "", thread_id: "workspaces",
      request_id: "", sequence: 2, type: "catalog/workspace-changed",
      data: { workspace: { ...first, title: "From frame", updated_at: "2026-01-02T00:00:00Z" } },
    });
    resolveList([{ ...first, title: "Stale baseline" }, second]);
    await refresh;

    expect(manager.getSnapshot().items.map((item) => item.title)).toEqual(["From frame", "Second"]);
  });

  it("accepts a later recreation of a removed deterministic Workspace id", async () => {
    const manager = new WorkspaceManager(fakeApi());
    await manager.refresh();
    manager.handleCatalogEvent({
      protocol_version: "xbotv2.v3", session_id: "", thread_id: "workspaces",
      request_id: "", sequence: 1, type: "catalog/workspace-removed",
      data: { workspace_id: "ws-1" },
    });
    manager.handleCatalogEvent({
      protocol_version: "xbotv2.v3", session_id: "", thread_id: "workspaces",
      request_id: "", sequence: 2, type: "catalog/workspace-changed",
      data: { workspace: { ...first, updated_at: "2026-01-02T00:00:00Z" } },
    });

    expect(manager.getSnapshot().items.map((item) => item.workspace_id)).toEqual(["ws-1", "ws-2"]);
  });
});

function workspace(workspaceId: string, title: string, updatedAt: string): WorkspaceData {
  return {
    workspace_id: workspaceId,
    path: `/workspace/${workspaceId}`,
    title,
    session_ids: [],
    created_at: "2026-01-01T00:00:00Z",
    updated_at: updatedAt,
  };
}

function fakeApi(overrides: Partial<Record<keyof ConstructorParameters<typeof WorkspaceManager>[0], unknown>> = {}) {
  return {
    listWorkspaces: async () => ({ items: [first, second], archived_session_ids: [], event_cursor: 0 }),
    createWorkspace: async () => first,
    renameWorkspace: async () => first,
    deleteWorkspace: async () => undefined,
    reorderWorkspace: async () => ["ws-2", "ws-1"],
    reorderWorkspaceSession: async () => first,
    setSessionArchived: async (_sessionId: string, archived: boolean) => archived ? ["session-1"] : [],
    ...overrides,
  } as ConstructorParameters<typeof WorkspaceManager>[0];
}
