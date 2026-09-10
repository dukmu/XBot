import { fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { ToolEntry } from "../state/runtime";
import { ToolCall } from "./ToolCall";

function editTool(overrides: Partial<ToolEntry> = {}): ToolEntry {
  return {
    id: "tool-1",
    kind: "tool",
    toolCallId: "call-1",
    name: "edit",
    args: { mode: "write", path: "src/new.py", content: "first\nsecond\n" },
    status: "success",
    result: "Wrote src/new.py",
    data: { changed: true, resolved_path: "/private/workspace/src/new.py" },
    error: null,
    artifacts: [],
    images: [],
    ...overrides,
  };
}

function openTool(tool: ToolEntry) {
  const view = render(<ToolCall tool={tool} />);
  const details = view.container.querySelector("details");
  if (!details) throw new Error("ToolCall did not render details");
  details.open = true;
  fireEvent(details, new Event("toggle"));
  return view;
}

describe("ToolCall file mutation presentation", () => {
  it("renders a successful write from model-facing arguments", () => {
    const view = openTool(editTool());
    const diff = view.container.querySelector<HTMLElement>("[data-diff]");
    if (!diff) throw new Error("Applied write did not render a diff");

    expect(within(diff).getByText("src/new.py", { exact: true })).toBeInTheDocument();
    expect(within(diff).getByText("first", { exact: true })).toBeInTheDocument();
    expect(within(diff).getByText("second", { exact: true })).toBeInTheDocument();
    expect(within(diff).getByText("└ +2 -0 · 1 file", { exact: true })).toBeInTheDocument();
    expect(screen.queryByText("/private/workspace/src/new.py", { exact: true })).toBeNull();
  });

  it("renders the removed and added sides of a successful replacement", () => {
    openTool(editTool({
      args: {
        mode: "replace",
        path: "src/app.py",
        old_text: "old value\nshared",
        new_text: "new value\nshared",
      },
    }));

    expect(screen.getByText("old value", { exact: true })).toBeInTheDocument();
    expect(screen.getByText("new value", { exact: true })).toBeInTheDocument();
    expect(screen.getByText("└ +2 -2 · 1 file", { exact: true })).toBeInTheDocument();
  });

  it("keeps authoritative Tool artifacts alongside an applied diff", () => {
    const view = openTool(editTool({
      artifacts: [{
        id: "tool_results/report.txt",
        name: "report.txt",
        media_type: "text/plain",
        size: 1536,
        url: "/sessions/demo/threads/main/artifacts/tool_results/report.txt",
      }],
    }));

    expect(view.container.querySelector("[data-diff]")).not.toBeNull();
    const artifact = screen.getByRole("link", { name: "Open artifact report.txt" });
    expect(artifact).toHaveAttribute("href", "/sessions/demo/threads/main/artifacts/tool_results/report.txt");
    expect(artifact).toHaveTextContent("report.txt");
    expect(artifact).toHaveTextContent("text/plain · 1.5 kB");
  });

  it("does not invent a download target for malformed artifact metadata", () => {
    openTool(editTool({ artifacts: [{ id: "tool_results/missing.txt", name: "missing.txt" }] }));

    expect(screen.queryByRole("link", { name: "Open artifact missing.txt" })).toBeNull();
  });

  it.each([
    ["failed", editTool({ status: "error", error: { code: "write_failed" } })],
    ["unchanged", editTool({ data: { changed: false } })],
    ["patch", editTool({ args: { mode: "patch", path: "src/app.py", patch: "@@ -1 +1 @@" } })],
  ])("keeps %s edits on the generic file-result path", (_label, tool) => {
    const view = openTool(tool);

    expect(within(view.container).queryByText(/^└ \+/u)).toBeNull();
    const header = view.container.querySelector<HTMLElement>(".tool-file-card header");
    if (!header) throw new Error("Generic file-result card did not render");
    expect(within(header).getByText("Path", { exact: true })).toBeInTheDocument();
    const path = String((tool.args as Record<string, unknown>).path);
    expect(within(header).getByText(path, { exact: true })).toBeInTheDocument();
  });
});
