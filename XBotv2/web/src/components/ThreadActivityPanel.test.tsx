import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { ThreadSummary } from "../api/types";
import { ThreadActivityPanel } from "./ThreadActivityPanel";

function thread(overrides: Partial<ThreadSummary>): ThreadSummary {
  return {
    session_id: "session-1",
    thread_id: "agent",
    status: "active",
    kind: "main",
    turn_status: "idle",
    parent_thread_id: "",
    agent: "default",
    provider: "mock",
    model: "mock-model",
    model_mode: "",
    context_window: 1000,
    message_count: 2,
    usage: { input_tokens: 0, output_tokens: 0, total_tokens: 12, requests: 1, context_tokens: 0, cache_read_input_tokens: 0, cache_creation_input_tokens: 0, prompt_cache_write_tokens: 0 },
    session_stats: { turns: 1, steps: 1, llm_ms: 0, tool_ms: 0, ttft_ms: 0, ttft_steps: 0, decode_ms: 0, decode_tokens: 0 },
    pending_interactions: [],
    status_slots: {},
    ...overrides,
  };
}

describe("ThreadActivityPanel", () => {
  it("keeps all resident threads visible and selects a child", () => {
    const onSelect = vi.fn();
    const child = thread({ thread_id: "worker-1", kind: "subagent", agent: "researcher", turn_status: "running" });
    render(<ThreadActivityPanel threads={[thread({}), child]} currentThreadId="agent" onSelect={onSelect} />);

    expect(screen.getByRole("button", { name: /researcher.*Running/i })).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: /researcher.*Running/i }));
    expect(onSelect).toHaveBeenCalledWith(child);
  });
});
