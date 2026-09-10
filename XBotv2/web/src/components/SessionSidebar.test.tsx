import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { SessionSummary, WorkspaceData } from "../api/types";
import { SessionSidebar } from "./SessionSidebar";

function session(index: number): SessionSummary {
  return {
    session_id: `session-${index}`,
    title: `Session ${index}`,
    status: "inactive",
    active_threads: 0,
    thread_count: 1,
    blank: false,
  };
}

describe("SessionSidebar", () => {
  it("bounds workspace rows until the user expands the group", () => {
    const sessions = Array.from({ length: 7 }, (_, index) => session(index + 1));
    const workspace: WorkspaceData = {
      workspace_id: "workspace-1",
      path: "/workspace",
      title: "Workspace",
      session_ids: sessions.map((item) => item.session_id),
      created_at: "2026-09-01T00:00:00Z",
      updated_at: "2026-09-01T00:00:00Z",
    };
    const noop = vi.fn();

    render(
      <SessionSidebar
        open
        collapsed={false}
        width={280}
        sessions={sessions}
        workspaces={[workspace]}
        archivedSessionIds={[]}
        threads={[]}
        current={null}
        onClose={noop}
        onToggle={noop}
        onSettings={noop}
        onNew={noop}
        onRefresh={async () => undefined}
        refreshing={false}
        onSession={noop}
        onThread={noop}
        onFork={noop}
        onDelete={noop}
        onRenameSession={noop}
        onArchiveSession={noop}
        onRenameWorkspace={noop}
        onDeleteWorkspace={noop}
        onMoveWorkspace={noop}
        onMoveSession={noop}
      />,
    );

    expect(screen.getByText("Session 5")).toBeVisible();
    expect(screen.queryByText("Session 6")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Show 2 more" }));
    expect(screen.getByText("Session 7")).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "Show less" }));
    expect(screen.queryByText("Session 6")).not.toBeInTheDocument();
  });
});
