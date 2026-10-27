import { fireEvent, render, screen } from "@testing-library/react";
import { expect, it, vi } from "vitest";
import { SessionSidebar } from "./SessionSidebar";
import type { SessionSummary, WorkspaceData } from "../api/types";

const session: SessionSummary = { session_id: "s-101", title: "Later session", status: "inactive", active_threads: 0, thread_count: 1, blank: false, workspace_root: "/w" };
const workspace: WorkspaceData = { workspace_id: "w", title: "Workspace", path: "/w", session_count: 201, created_at: "", updated_at: "" };

it("navigates the bounded workspace and member pages and selects a session", () => {
  const noop = vi.fn();
  const select = vi.fn();
  const open = vi.fn();
  const nextSessions = vi.fn();
  const previousSessions = vi.fn();
  render(<SessionSidebar open collapsed={false} width={280}
    catalog={{ items: [session], archivedIds: [], scope: { kind: "workspace", workspaceId: "w" }, canPrevious: true, canNext: true, eventCursor: 0, state: "idle", error: null }}
    workspacePage={{ items: [workspace], canPrevious: false, canNext: true, eventCursor: 0, state: "idle", error: null }}
    threads={[]} threadPreviousCursor={null} threadNextCursor={null} threadPageLoading={false} onPageThreads={noop}
    current={{ session_id: "off-page", thread_id: "main" }} onClose={noop} onToggle={noop} onSettings={noop} onNew={noop} onRefresh={async () => undefined} refreshing={false}
    onSession={open} onThread={noop} onFork={noop} onDelete={noop} onRenameSession={noop} onArchiveSession={noop} onRenameWorkspace={noop} onDeleteWorkspace={noop} onMoveWorkspace={noop} onMoveSession={noop}
    onSelectCatalog={select} onNextSessions={nextSessions} onPreviousSessions={previousSessions} onNextWorkspaces={noop} onPreviousWorkspaces={noop} />);
  expect(screen.getByText("201")).toBeVisible();
  fireEvent.click(screen.getByText("Later session"));
  expect(open).toHaveBeenCalledWith("s-101");
  fireEvent.click(screen.getByRole("button", { name: "Next sessions" }));
  fireEvent.click(screen.getByRole("button", { name: "Previous sessions" }));
  expect(nextSessions).toHaveBeenCalledOnce();
  expect(previousSessions).toHaveBeenCalledOnce();
  fireEvent.click(screen.getByRole("button", { name: "Archived" }));
  expect(select).toHaveBeenCalledWith({ kind: "archived" });
});
