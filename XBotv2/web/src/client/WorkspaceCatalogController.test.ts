import { describe, expect, it, vi } from "vitest";
import type { ServerEvent, SessionSummary, WorkspaceData } from "../api/types";
import { WorkspaceCatalogController } from "./WorkspaceCatalogController";
import { SessionCatalog } from "./SessionCatalog";
import { WorkspaceManager } from "./WorkspaceManager";

describe("WorkspaceCatalogController", () => {
  it("connects from the oldest baseline and projects replayed frames", async () => {
    const session = sessionSummary("Before");
    const workspace = workspaceData();
    const cursors: number[] = [];
    const api = {
      listSessions: async () => ({ sessions: [session], event_cursor: 4 }),
      renameSession: async () => session,
      listWorkspaces: async () => ({ items: [workspace], archived_session_ids: [], event_cursor: 6 }),
      createWorkspace: async () => workspace,
      renameWorkspace: async () => workspace,
      deleteWorkspace: async () => undefined,
      reorderWorkspace: async () => [workspace.workspace_id],
      reorderWorkspaceSession: async () => workspace,
      setSessionArchived: async () => [],
      async *streamWorkspaceEvents(after: number): AsyncGenerator<ServerEvent> {
        cursors.push(after);
        yield catalogSessionFrame(5, "After");
        await new Promise(() => undefined);
      },
    };
    const sessions = new SessionCatalog(api);
    const workspaces = new WorkspaceManager(api);
    const controller = new WorkspaceCatalogController(api, sessions, workspaces, {
      onConnection: vi.fn(),
      onError: vi.fn(),
    });

    await controller.start();
    await vi.waitFor(() => expect(sessions.getSnapshot().items[0]?.title).toBe("After"));

    expect(cursors).toEqual([4]);
    expect(sessions.getSnapshot().eventCursor).toBe(5);
    expect(workspaces.getSnapshot().eventCursor).toBe(6);
    controller.stop();
  });

  it("reuses only an accounted, unarchived blank session", async () => {
    const blank = { ...sessionSummary("Blank"), blank: true };
    const workspace = workspaceData();
    const api = {
      listSessions: async () => ({ sessions: [blank], event_cursor: 0 }),
      renameSession: async () => blank,
      listWorkspaces: async () => ({ items: [workspace], archived_session_ids: [], event_cursor: 0 }),
      createWorkspace: async () => workspace,
      renameWorkspace: async () => workspace,
      deleteWorkspace: async () => undefined,
      reorderWorkspace: async () => [workspace.workspace_id],
      reorderWorkspaceSession: async () => workspace,
      setSessionArchived: async () => [],
      async *streamWorkspaceEvents(): AsyncGenerator<ServerEvent> {
        await new Promise(() => undefined);
      },
    };
    const sessions = new SessionCatalog(api);
    const workspaces = new WorkspaceManager(api);
    const controller = new WorkspaceCatalogController(api, sessions, workspaces, {
      onConnection: vi.fn(), onError: vi.fn(),
    });
    await Promise.all([sessions.refresh(), workspaces.refresh()]);

    expect(controller.reusableSession("/workspace")).toEqual(blank);
    expect(controller.reusableSession("/other")).toBeUndefined();
    workspaces.handleCatalogEvent({
      protocol_version: "xbotv2.v3", session_id: "", thread_id: "workspaces",
      request_id: "", sequence: 1, type: "catalog/archived-sessions-changed",
      data: { archived_session_ids: [blank.session_id] },
    });
    expect(controller.reusableSession("/workspace")).toBeUndefined();
  });
});

function sessionSummary(title: string): SessionSummary {
  return {
    session_id: "session-1",
    title,
    workspace_root: "/workspace",
    status: "inactive",
    active_threads: 0,
    thread_count: 1,
    blank: false,
  };
}

function workspaceData(): WorkspaceData {
  return {
    workspace_id: "workspace-1",
    path: "/workspace",
    title: "Workspace",
    session_ids: ["session-1"],
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
  };
}

function catalogSessionFrame(sequence: number, title: string): ServerEvent {
  return {
    protocol_version: "xbotv2.v3",
    session_id: "",
    thread_id: "workspaces",
    request_id: "",
    sequence,
    type: "catalog/session-changed",
    data: { session: sessionSummary(title) },
  };
}
