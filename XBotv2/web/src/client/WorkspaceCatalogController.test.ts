import { expect, it, vi } from "vitest";
import { WorkspaceCatalogController } from "./WorkspaceCatalogController";
import { SessionCatalog } from "./SessionCatalog";
import { WorkspaceManager } from "./WorkspaceManager";

it("subscribes from the older page snapshot cursor", async () => {
  const cursors: number[] = [];
  const api = {
    listSessions: vi.fn(async () => ({ sessions: [], previous_cursor: null, next_cursor: null, event_cursor: 4 })),
    listWorkspaceSessions: vi.fn(), listArchivedSessions: vi.fn(), renameSession: vi.fn(),
    listWorkspaces: vi.fn(async () => ({ items: [], previous_cursor: null, next_cursor: null, event_cursor: 6 })),
    createWorkspace: vi.fn(), renameWorkspace: vi.fn(), deleteWorkspace: vi.fn(), reorderWorkspace: vi.fn(), reorderWorkspaceSession: vi.fn(), setSessionArchived: vi.fn(),
    async *streamWorkspaceEvents(after: number): AsyncGenerator<import("../api/wire").ServerEvent> { cursors.push(after); await new Promise(() => undefined); },
  };
  const controller = new WorkspaceCatalogController(api, new SessionCatalog(api), new WorkspaceManager(api), { onConnection: vi.fn(), onError: vi.fn() });
  await controller.start();
  await vi.waitFor(() => expect(cursors).toEqual([4]));
  controller.stop();
});

it("refreshes both pages after event cursor expiry and stops cleanly", async () => {
  const { XBotApiError } = await import("../api/client");
  const cursors: number[] = [];
  let version = 0;
  const api = {
    listSessions: vi.fn(async () => ({ sessions: [], previous_cursor: null, next_cursor: null, event_cursor: version ? 10 : 1 })),
    listWorkspaceSessions: vi.fn(),
    listArchivedSessions: vi.fn(),
    renameSession: vi.fn(),
    listWorkspaces: vi.fn(async () => ({ items: [], previous_cursor: null, next_cursor: null, event_cursor: version ? 10 : 1 })),
    createWorkspace: vi.fn(),
    renameWorkspace: vi.fn(),
    deleteWorkspace: vi.fn(),
    reorderWorkspace: vi.fn(),
    reorderWorkspaceSession: vi.fn(),
    setSessionArchived: vi.fn(),
    async *streamWorkspaceEvents(after: number): AsyncGenerator<import("../api/wire").ServerEvent> {
      cursors.push(after);
      if (version++ === 0) throw new XBotApiError(409, "workspace_event_cursor_expired", "expired", true);
      await new Promise(() => undefined);
    },
  };
  const controller = new WorkspaceCatalogController(
    api,
    new SessionCatalog(api),
    new WorkspaceManager(api),
    { onConnection: vi.fn(), onError: vi.fn() },
  );
  await controller.start();
  await vi.waitFor(() => expect(cursors).toEqual([1, 10]));
  expect(api.listSessions).toHaveBeenCalledTimes(2);
  expect(api.listWorkspaces).toHaveBeenCalledTimes(2);
  controller.stop();
});
