import { fireEvent, render } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { ToolCall } from "./ToolCall";
import type { ToolEntry } from "../state/runtime";

const tool: ToolEntry = {
  id: "trajectory:3:tool:0",
  kind: "tool",
  toolCallId: "call-1",
  name: "shell",
  args: { command: "pwd" },
  status: "success",
  result: "/workspace",
  data: null,
  error: null,
  artifacts: [],
  images: [],
};

describe("ToolCall selection", () => {
  it("reports the row it belongs to when the reader picks it", () => {
    // The details column follows this callback.
    const onSelect = vi.fn();
    const { getByTestId } = render(<ToolCall tool={tool} onSelect={onSelect} />);
    getByTestId("tool-row-call-1").click();
    expect(onSelect).toHaveBeenCalledWith(tool);
  });

  it("marks itself as the row the details column is showing", () => {
    const { container } = render(<ToolCall tool={tool} selected />);
    expect(container.querySelector(".tool-block-selected")).not.toBeNull();
  });
});

describe("to-do row", () => {
  const todos: ToolEntry = {
    ...tool,
    name: "task_update",
    data: {
      kind: "todo_snapshot",
      tasks: [
        { id: "1", subject: "梳理需求", status: "completed" },
        { id: "2", subject: "实现 fixture 样本", status: "in_progress", activeForm: "实现 fixture 样本" },
        { id: "3", subject: "跑后台构建", status: "in_progress" },
        { id: "4", subject: "浏览器验收", status: "pending" },
      ],
    },
  };

  it("titles the row, summarises it and counts the remaining active items", () => {
    // Layout and wording follow the ported to-do row: title, "n/m completed ·
    // <active>", and "+N" for the other in-progress items.
    const { getByText } = render(<ToolCall tool={todos} />);
    expect(getByText("Update to-do list")).toBeTruthy();
    expect(getByText("1/4 completed · 实现 fixture 样本 +1")).toBeTruthy();
  });

  it("tallies the states and lists one line per item when expanded", () => {
    const { container, getByTestId, getByText } = render(<ToolCall tool={todos} />);
    // jsdom does not open a <details> on a summary click, so drive the toggle
    // the component actually listens to.
    const details = container.querySelector("details") as HTMLDetailsElement;
    details.open = true;
    fireEvent(details, new Event("toggle"));
    expect(getByTestId("todo-tally").textContent).toBe("1 completed · 2 in progress · 1 pending");
    expect(getByText("completed 梳理需求")).toBeTruthy();
    expect(getByText("pending 浏览器验收")).toBeTruthy();
  });
});

/**
 * The ported skill field: dsh renders a skill load as `Skill <name>` with the
 * loaded instructions in a labelled region and an `Inspect` way into the full
 * call record. XBot names the tool after the skill and passes no arguments, so
 * the name is the row's summary.
 */
describe("skill row", () => {
  const skillTool: ToolEntry = {
    ...tool,
    toolCallId: "call-skill",
    name: "snapshot-skill",
    args: {},
    status: "success",
    result: "<skill_content name=\"snapshot-skill\">\nFollow these snapshot-only instructions.\n</skill_content>",
  };

  it("shows Skill plus the skill name and opens the instructions on arrival", () => {
    const { container } = render(<ToolCall tool={skillTool} skill />);
    expect(container.querySelector(".tool-name")?.textContent).toBe("Skill");
    expect(container.querySelector(".tool-summary")?.textContent).toBe("snapshot-skill");
    const region = container.querySelector(".skill-instructions");
    expect(region?.getAttribute("aria-label")).toBe("Instructions");
    expect(region?.textContent).toContain("Follow these snapshot-only instructions.");
  });

  it("keeps the generic row for a tool the catalog does not mark as a skill", () => {
    const { container } = render(<ToolCall tool={skillTool} />);
    expect(container.querySelector(".tool-name")?.textContent).toBe("snapshot-skill");
    expect(container.querySelector(".skill-instructions")).toBeNull();
  });

  it("inspects the call through the details column", () => {
    const onSelect = vi.fn();
    const { getByRole } = render(<ToolCall tool={skillTool} skill onSelect={onSelect} />);
    fireEvent.click(getByRole("button", { name: "Inspect" }));
    expect(onSelect).toHaveBeenCalledWith(skillTool);
  });
});

/**
 * A search call keeps its structured result: the ported card groups the matches
 * instead of dumping the tool's JSON into the row.
 */
describe("search row", () => {
  const searchTool: ToolEntry = {
    ...tool,
    toolCallId: "call-search",
    name: "search",
    args: { pattern: "SearchBlock", path: "src" },
    status: "success",
    result: "{\"returned_matches\": 2}",
    data: {
      kind: "directory",
      pattern: "SearchBlock",
      returned_matches: 2,
      truncated: false,
      matches: [
        { path: "src/SearchBlock.tsx", line: 16, column: 1, text: "export const DEFAULT_SEARCH_MAX_LINES = 16" },
        { path: "src/search-row.tsx", line: 34, column: 1, text: "export function SearchRow({" },
      ],
    },
  };

  it("renders grouped matches from the structured result", () => {
    const { container } = render(<ToolCall tool={searchTool} />);
    // jsdom does not open a <details> on a summary click, so drive the toggle.
    const details = container.querySelector("details")!;
    details.open = true;
    fireEvent(details, new Event("toggle"));
    expect(container.querySelector(".search-card")).not.toBeNull();
    expect(container.querySelector(".search-summary")?.textContent).toBe("2 matches · 2 files");
    expect(container.textContent).toContain("16: export const DEFAULT_SEARCH_MAX_LINES = 16");
  });

  it("falls back to the generic result when the search carries no structured matches", () => {
    const { container } = render(<ToolCall tool={{ ...searchTool, data: { ok: false, error: "boom" } }} />);
    const details = container.querySelector("details")!;
    details.open = true;
    fireEvent(details, new Event("toggle"));
    expect(container.querySelector(".search-card")).toBeNull();
    expect(container.textContent).toContain("boom");
  });
});
