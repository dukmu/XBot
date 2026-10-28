import { expect, it, vi } from "vitest";
import { WorkspaceManager } from "./WorkspaceManager";
import type { WorkspaceData } from "../api/types";
const workspace = (id: string): WorkspaceData => ({ workspace_id: id, title: id, path: `/${id}`, session_count: 2, created_at: "", updated_at: "" });

it("keeps one workspace page and uses relative moves at a page boundary", async () => {
  const reorderWorkspace = vi.fn(async (id: string, before: string | null) => ({ workspace_id: id, before_workspace_id: before }));
  const reorderWorkspaceSession = vi.fn(async () => workspace("a"));
  const listWorkspaces = vi.fn(async (cursor?: string | null) => cursor === "next"
    ? { items: [workspace("c")], previous_cursor: "prev", next_cursor: null, event_cursor: 0 }
    : { items: [workspace("a"), workspace("b")], previous_cursor: null, next_cursor: "next", event_cursor: 0 });
  const manager = new WorkspaceManager({ listWorkspaces, reorderWorkspace, reorderWorkspaceSession, createWorkspace: vi.fn(), renameWorkspace: vi.fn(), deleteWorkspace: vi.fn(), setSessionArchived: vi.fn() });
  await manager.refresh();
  await manager.move("a", 1);
  expect(reorderWorkspace).toHaveBeenCalledWith("b", "a");
  await manager.moveSession("a", ["one", "two"], "one", 1);
  expect(reorderWorkspaceSession).toHaveBeenCalledWith("a", "two", "one");
  await manager.next();
  expect(manager.getSnapshot()).toMatchObject({ items: [workspace("c")], canPrevious: true, canNext: false });
  await manager.back();
  expect(manager.getSnapshot().items).toEqual([workspace("a"), workspace("b")]);
  expect(listWorkspaces.mock.calls.at(-1)?.[0]).toBe("prev");
});


it("refreshes after deletion while an older page is still in flight", async () => {
  let resolve!: (value: { items: WorkspaceData[]; previous_cursor: null; next_cursor: null; event_cursor: number }) => void;
  const listWorkspaces = vi.fn()
    .mockImplementationOnce(() => new Promise((done) => { resolve = done; }))
    .mockResolvedValue({ items: [], previous_cursor: null, next_cursor: null, event_cursor: 1 });
  const manager = new WorkspaceManager({ listWorkspaces, createWorkspace: vi.fn(), renameWorkspace: vi.fn(), deleteWorkspace: vi.fn(async () => undefined), reorderWorkspace: vi.fn(), reorderWorkspaceSession: vi.fn(), setSessionArchived: vi.fn() });
  const first = manager.refresh();
  const removed = manager.delete("a");
  await Promise.resolve();
  resolve({ items: [workspace("a")], previous_cursor: null, next_cursor: null, event_cursor: 0 });
  await Promise.all([first, removed]);
  expect(manager.getSnapshot().items).toEqual([]);
  expect(listWorkspaces).toHaveBeenCalledTimes(2);
});

it("keeps the previous cursor when the last workspace page becomes empty", async () => {
  const listWorkspaces = vi.fn(async (cursor?: string | null) => cursor === "next"
    ? { items: [], previous_cursor: "prev", next_cursor: null, event_cursor: 0 }
    : { items: [workspace("a")], previous_cursor: null, next_cursor: "next", event_cursor: 0 });
  const manager = new WorkspaceManager({ listWorkspaces, createWorkspace: vi.fn(), renameWorkspace: vi.fn(), deleteWorkspace: vi.fn(), reorderWorkspace: vi.fn(), reorderWorkspaceSession: vi.fn(), setSessionArchived: vi.fn() });
  await manager.refresh();
  await manager.next();
  expect(manager.getSnapshot()).toMatchObject({ items: [], canPrevious: true });
  await manager.back();
  expect(manager.getSnapshot().items).toEqual([workspace("a")]);
});
