import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { TrajectoryItem } from "../api/types";
import {
  CALLS_LABEL,
  DURATION_LABEL,
  TRAJECTORY_SEARCH,
  TRAJECTORY_TIMELINE,
  TRAJECTORY_TOOLBAR,
  TURNS_LABEL,
  TrajectoryPane,
  formatTiming,
  trajectoryRows,
  visibleRows,
} from "./TrajectoryPane";

function message(
  position: number,
  role: "user" | "assistant" | "tool",
  content: string,
  extra: Record<string, unknown> = {},
): TrajectoryItem {
  return {
    position,
    kind: "message",
    message_id: `m-${position}`,
    message: {
      role,
      content,
      tool_calls: [],
      tool_call_id: "",
      status: "",
      data: null,
      error: null,
      artifacts: [],
      images: [],
      ...extra,
    },
  } as unknown as TrajectoryItem;
}

const items: TrajectoryItem[] = [
  { position: 1, kind: "event", event: "turn_started", data: { turn: 1 }, timestamp: "" } as unknown as TrajectoryItem,
  message(2, "user", "Use the read tool twice"),
  message(3, "assistant", "I will read both files", {
    tool_calls: [{ name: "read", arguments: { path: "a.txt" } }],
    timing: { llm_ms: 1542, ttft_ms: 368, decode_ms: 1174 },
  }),
  message(4, "tool", "alpha", { tool_call_id: "call-1" }),
  message(5, "assistant", "DONE"),
];

/**
 * The ported trajectory pane: a toolbar (duration / turns / calls and the
 * trajectory search box) over one record table, with turn and request labels in
 * the role cell and the timing line where the record carries one.
 */
describe("TrajectoryPane", () => {
  it("renders the ported toolbar and one table of records", () => {
    render(<TrajectoryPane items={items} />);
    expect(screen.getByRole("toolbar", { name: TRAJECTORY_TOOLBAR })).toBeTruthy();
    expect(screen.getByRole("region", { name: TRAJECTORY_TIMELINE })).toBeTruthy();
    expect(screen.getByRole("searchbox", { name: TRAJECTORY_SEARCH })).toBeTruthy();
    expect(screen.getByRole("button", { name: DURATION_LABEL }).getAttribute("aria-pressed")).toBe("true");
    expect(screen.getByRole("button", { name: TURNS_LABEL }).getAttribute("aria-pressed")).toBe("false");
    expect(screen.getByRole("button", { name: CALLS_LABEL }).getAttribute("aria-pressed")).toBe("false");
    // Lifecycle events drive the numbering; the table lists records only.
    expect(screen.getAllByRole("row")).toHaveLength(4);
    expect(screen.getByText("Request #1")).toBeTruthy();
    expect(screen.getByText("Total 1,542 ms · TTFT 368 ms · Decoding 1,174 ms")).toBeTruthy();
  });

  it("selects a tool record through the row", () => {
    const onSelect = vi.fn();
    render(<TrajectoryPane items={items} onSelect={onSelect} />);
    fireEvent.click(screen.getByText("alpha"));
    expect(onSelect).toHaveBeenCalledWith(expect.objectContaining({
      position: 4,
      role: "TOOL",
      toolCallId: "call-1",
    }));
    expect(screen.getByText("alpha").closest("tr")?.getAttribute("aria-selected")).toBe(null);
  });

  it("filters records from the trajectory search box", () => {
    render(<TrajectoryPane items={items} />);
    fireEvent.change(screen.getByRole("searchbox", { name: TRAJECTORY_SEARCH }), {
      target: { value: "alpha" },
    });
    expect(screen.getAllByRole("row")).toHaveLength(1);
    expect(screen.getByText("alpha")).toBeTruthy();
    fireEvent.change(screen.getByRole("searchbox", { name: TRAJECTORY_SEARCH }), { target: { value: "zzz" } });
    expect(screen.queryAllByRole("row")).toHaveLength(0);
    expect(screen.getByText("No matching records")).toBeTruthy();
  });

  it("collapses turns and calls from the toolbar", () => {
    render(<TrajectoryPane items={items} />);
    fireEvent.click(screen.getByRole("button", { name: TURNS_LABEL }));
    const rows = screen.getAllByRole("row");
    // One summary row for turn 1: its four records are behind it.
    expect(rows).toHaveLength(1);
    expect(screen.getByText("4 records · 2 requests")).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: TURNS_LABEL }));
    fireEvent.click(screen.getByRole("button", { name: CALLS_LABEL }));
    // Collapsing calls drops the tool row.
    expect(screen.queryByText("alpha")).toBeNull();
  });

  it("hides timing lines when duration is off", () => {
    render(<TrajectoryPane items={items} />);
    fireEvent.click(screen.getByRole("button", { name: DURATION_LABEL }));
    expect(screen.queryByText(/Total 1,542 ms/)).toBeNull();
  });
});

describe("trajectory projections", () => {
  it("numbers turns and requests from the trajectory's own boundaries", () => {
    const rows = trajectoryRows(items);
    expect(rows.map((row) => [row.position, row.turn, row.request, row.role])).toEqual([
      [2, 1, 0, "USER"],
      [3, 1, 1, "ASSISTANT"],
      [4, 1, 0, "TOOL"],
      [5, 1, 2, "ASSISTANT"],
    ]);
    expect(rows[2].toolCallId).toBe("call-1");
  });

  it("formats the ported timing line and nothing when there is none", () => {
    expect(formatTiming({ llm_ms: 1542, ttft_ms: 368, decode_ms: 1174 }))
      .toBe("Total 1,542 ms · TTFT 368 ms · Decoding 1,174 ms");
    expect(formatTiming({ duration_ms: 45 })).toBe("Total 45 ms");
    expect(formatTiming(null)).toBe("");
  });

  it("keeps records without a turn when collapsing turns", () => {
    const loose = [message(1, "user", "before any turn")];
    const rows = visibleRows(trajectoryRows(loose), { query: "", collapseTurns: true, collapseCalls: false });
    expect(rows).toHaveLength(1);
    expect(rows[0].role).toBe("USER");
  });
});
