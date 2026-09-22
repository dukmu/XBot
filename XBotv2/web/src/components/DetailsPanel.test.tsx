import { render } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { DETAILS_EMPTY_HINT, DetailsPanel } from "./DetailsPanel";
import type { ToolEntry } from "../state/runtime";

const tool: ToolEntry = {
  id: "trajectory:3:tool:0",
  kind: "tool",
  toolCallId: "call-1",
  name: "filesystem_read",
  args: { path: "notes.md" },
  status: "success",
  result: "file contents",
  data: null,
  error: null,
  artifacts: [],
  images: [],
};

describe("DetailsPanel", () => {
  it("explains itself until a tool row is picked", () => {
    // Copy ported verbatim from the DeepSeek Harness details column.
    const { getByText, queryByText } = render(<DetailsPanel tool={null} onClose={() => undefined} />);
    expect(getByText(DETAILS_EMPTY_HINT)).toBeTruthy();
    expect(queryByText("Arguments")).toBeNull();
  });

  it("shows the picked tool's arguments and result", () => {
    const { getByText } = render(<DetailsPanel tool={tool} onClose={() => undefined} />);
    expect(getByText("filesystem_read")).toBeTruthy();
    expect(getByText("Arguments")).toBeTruthy();
    expect(getByText("Result")).toBeTruthy();
  });

  it("closes from its header", () => {
    const onClose = vi.fn();
    const { getByLabelText } = render(<DetailsPanel tool={tool} onClose={onClose} />);
    getByLabelText("Close details").click();
    expect(onClose).toHaveBeenCalledTimes(1);
  });
});

/**
 * A search call shows its grouped card here as well — the ported model derives
 * the card once for the chat row and this column.
 */
describe("details panel search card", () => {
  it("renders the grouped matches with the panel's own cap", () => {
    const search = {
      ...tool,
      toolCallId: "call-search",
      name: "search",
      args: { pattern: "needle" },
      status: "success",
      result: "{\"returned_matches\": 1}",
      data: {
        kind: "directory",
        returned_matches: 1,
        truncated: false,
        matches: [{ path: "a.ts", line: 3, column: 1, text: "needle here" }],
      },
    };
    const { container } = render(<DetailsPanel tool={search} onClose={() => undefined} />);
    expect(container.querySelector(".search-card")).not.toBeNull();
    expect(container.querySelector(".search-summary")?.textContent).toBe("1 match · 1 file");
    // The raw JSON result is replaced, not repeated.
    expect(container.textContent).not.toContain("returned_matches");
  });
});
