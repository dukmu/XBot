import { fireEvent, render } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { SubagentSessions, subagentTree } from "./SubagentSessions";
import type { ThreadSummary } from "../api/types";

function thread(over: Partial<ThreadSummary> & { thread_id: string }): ThreadSummary {
  return {
    session_id: "s",
    status: "active",
    kind: "subagent",
    turn_status: "idle",
    parent_thread_id: "agent",
    agent: over.thread_id,
    provider: "p",
    model: "m",
    model_mode: "",
    context_window: 1000,
    message_count: 1,
    usage: { input_tokens: 10, output_tokens: 5, total_tokens: 15, context_tokens: 0, cached_tokens: 0, reasoning_tokens: 0 },
    session_stats: {
      turns: 0, steps: 0, llm_ms: 0, tool_ms: 0, ttft_ms: 0, ttft_steps: 0, decode_ms: 0, decode_tokens: 0,
    },
    pending_interactions: [],
    status_slots: {},
    title: over.thread_id,
    ...over,
  } as unknown as ThreadSummary;
}

describe("subagentTree", () => {
  it("nests descendants and keeps orphans reachable", () => {
    const tree = subagentTree([
      thread({ thread_id: "a" }),
      thread({ thread_id: "b", parent_thread_id: "a" }),
      thread({ thread_id: "c", parent_thread_id: "b" }),
      thread({ thread_id: "orphan", parent_thread_id: "missing" }),
      thread({ thread_id: "main", kind: "main", parent_thread_id: "" }),
    ]);
    expect(tree.map((node) => node.thread.thread_id)).toEqual(["a", "orphan"]);
    expect(tree[0].children[0].thread.thread_id).toBe("b");
    expect(tree[0].children[0].children[0].thread.thread_id).toBe("c");
  });
});

describe("SubagentSessions", () => {
  const threads = [
    thread({ thread_id: "reviewer", turn_status: "running", usage: { total_tokens: 7900 } as never }),
    thread({ thread_id: "editor", parent_thread_id: "reviewer" }),
  ];

  it("lists sessions with state and token use", () => {
    const { getByTestId, getByText } = render(
      <SubagentSessions threads={threads} currentThreadId="reviewer" onSelect={() => undefined} />,
    );
    expect(getByTestId("subagent-reviewer")).toBeTruthy();
    expect(getByTestId("subagent-editor")).toBeTruthy();
    expect(getByText("2 subagents")).toBeTruthy();
    expect(getByText(/7,900 tok/)).toBeTruthy();
  });

  it("selects a session and collapses a branch", () => {
    const onSelect = vi.fn();
    const { getByTestId, getByLabelText, queryByTestId } = render(
      <SubagentSessions threads={threads} currentThreadId="agent" onSelect={onSelect} />,
    );
    fireEvent.click(getByTestId("subagent-reviewer"));
    expect(onSelect).toHaveBeenCalledWith(expect.objectContaining({ thread_id: "reviewer" }));

    fireEvent.click(getByLabelText("Collapse reviewer descendants"));
    expect(queryByTestId("subagent-editor")).toBeNull();
  });

  it("renders nothing when the session has no subagents", () => {
    const { container } = render(
      <SubagentSessions
        threads={[thread({ thread_id: "main", kind: "main", parent_thread_id: "" })]}
        currentThreadId="main"
        onSelect={() => undefined}
      />,
    );
    expect(container.querySelector(".subagent-sessions")).toBeNull();
  });

  it("shows only a count button until the reader opens the tree", () => {
    const { getByText, queryByTestId, getByRole } = render(
      <SubagentSessions
        threads={threads}
        currentThreadId="agent"
        defaultOpen={false}
        onSelect={() => undefined}
      />,
    );
    expect(queryByTestId("subagent-reviewer")).toBeNull();
    fireEvent.click(getByText("2 subagents"));
    expect(getByRole("treeitem", { name: /reviewer/ })).toBeTruthy();
  });
});
