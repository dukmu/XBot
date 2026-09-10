import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { UsageData } from "../api/types";
import { UsageStatsLine } from "./UsageStatsLine";

function usage(overrides: Partial<UsageData> = {}): UsageData {
  return {
    input_tokens: 0,
    output_tokens: 0,
    total_tokens: 0,
    requests: 0,
    context_tokens: 0,
    cache_read_input_tokens: 0,
    cache_creation_input_tokens: 0,
    prompt_cache_write_tokens: 0,
    ...overrides,
  };
}

describe("UsageStatsLine", () => {
  it("renders no synthetic statistics before usage exists", () => {
    const view = render(<UsageStatsLine usage={usage()} />);
    expect(view.container).toBeEmptyDOMElement();
  });

  it("uses the provider-neutral billing buckets for cache hit and token totals", () => {
    render(<UsageStatsLine usage={usage({
      input_tokens: 200,
      output_tokens: 75,
      cache_read_input_tokens: 700,
      cache_creation_input_tokens: 50,
      prompt_cache_write_tokens: 50,
    })} />);

    const stats = screen.getByRole("status", { name: "Session usage" });
    expect(stats).toHaveTextContent("Cache hit 70%");
    expect(stats).toHaveTextContent("Input 1K · Output 75");
  });

  it("formats durable model and tool timing without deriving it from the browser", () => {
    render(<UsageStatsLine usage={usage()} stats={{
      turns: 3,
      steps: 5,
      llm_ms: 65_200,
      tool_ms: 2_400,
      ttft_ms: 900,
      ttft_steps: 3,
      decode_ms: 4_000,
      decode_tokens: 100,
    }} />);

    const stats = screen.getByRole("status", { name: "Session usage" });
    expect(stats).toHaveTextContent("3 turns · 5 steps");
    expect(stats).toHaveTextContent("LLM 1m5s · Tool 2.4s");
    expect(stats).toHaveTextContent("TTFT 0.3s avg · 25 tok/s");
  });
});
