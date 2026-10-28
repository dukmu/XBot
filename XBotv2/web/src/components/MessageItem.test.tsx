import { render } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

// Count parses through the real component boundary: the counter only moves when
// the mocked Markdown child actually renders, so it measures both whether the
// message re-rendered and whether the parsed element was reused.
const markdownRenders = { count: 0 };
vi.mock("react-markdown", () => ({
  default: ({ children }: { children?: unknown }) => {
    markdownRenders.count += 1;
    return <div data-testid="markdown">{String(children ?? "")}</div>;
  },
}));

import { MessageItem, OUTPUT_LIMIT_HINT, OUTPUT_LIMIT_TITLE, formatTurnDuration } from "./MessageItem";
import type { TimelineEntry } from "../state/runtime";

type MessageEntry = Extract<TimelineEntry, { kind: "message" }>;

function entry(content: string): MessageEntry {
  return {
    id: "entry-1",
    kind: "message",
    role: "assistant",
    content,
    reasoning: "",
    streaming: false,
    messageId: "m1",
    images: [],
    deliveryState: undefined,
  } as unknown as MessageEntry;
}

describe("MessageItem content", () => {
  it("renders what the user typed", () => {
    // The memo around the parsed element must not shortcut a role: a user
    // message that renders nothing is invisible input.
    const message = { ...entry("what the user typed"), role: "user" as const };
    const { getByTestId } = render(<MessageItem entry={message} />);
    expect(getByTestId("markdown").textContent).toBe("what the user typed");
  });
});

describe("MessageItem render cost", () => {
  it("re-parses only when the message text changes", () => {
    markdownRenders.count = 0;
    const message = entry("# heading\n\nbody");
    const { rerender } = render(<MessageItem entry={message} />);
    expect(markdownRenders.count).toBe(1);

    // A real prop change forces the component to render; the parsed element
    // must still be reused because the text did not change.
    rerender(<MessageItem entry={message} branchUnavailable />);
    expect(markdownRenders.count).toBe(1);

    rerender(<MessageItem entry={entry("# heading\n\nbody!")} />);
    expect(markdownRenders.count).toBe(2);
  });

  it("does not render when only handler identities change", () => {
    markdownRenders.count = 0;
    const message = entry("content");
    const { rerender } = render(
      <MessageItem entry={message} onBranch={() => Promise.resolve()} />,
    );
    expect(markdownRenders.count).toBe(1);

    rerender(
      <MessageItem entry={message} onBranch={() => Promise.resolve()} />,
    );
    expect(markdownRenders.count).toBe(1);
  });
});

describe("output limit notice", () => {
  it("flags a reply the provider cut off, and only those", () => {
    // Ported wording from the max-tokens notice.
    const cut = { ...entry("half an answer"), stopReason: "length" };
    const cutRender = render(<MessageItem entry={cut} />);
    expect(cutRender.getByTestId("output-limit-notice")).toBeTruthy();
    expect(cutRender.getByText(OUTPUT_LIMIT_TITLE)).toBeTruthy();
    expect(cutRender.getByText(OUTPUT_LIMIT_HINT)).toBeTruthy();
    cutRender.unmount();

    const finished = { ...entry("a full answer"), stopReason: "end_turn" };
    const finishedRender = render(<MessageItem entry={finished} />);
    expect(finishedRender.queryByTestId("output-limit-notice")).toBeNull();
  });

  it("never shows it on a user message", () => {
    const message = { ...entry("typed by me"), role: "user" as const, stopReason: "length" };
    const { queryByTestId } = render(<MessageItem entry={message} />);
    expect(queryByTestId("output-limit-notice")).toBeNull();
  });
});

/**
 * The ported turn footer: what a settled reply ran for, from the timing the
 * durable record carries.  dsh's footer also carries the clock and throughput;
 * XBot messages have no timestamp and usage is per session, so those are absent
 * rather than invented.
 */
describe("turn footer", () => {
  it("shows the reply's own durations", () => {
    const { container } = render(
      <MessageItem entry={{ ...entry("done"), timing: { llm_ms: 1542, ttft_ms: 368, decode_ms: 1174 } } as never} />,
    );
    expect(container.querySelector("[data-testid='turn-footer']")?.textContent)
      .toBe("Ran for 1.5 s · TTFT 368 ms");
  });

  it("stays off a reply with no timing and off a user message", () => {
    const idle = render(<MessageItem entry={entry("done")} />);
    expect(idle.container.querySelector("[data-testid='turn-footer']")).toBeNull();
    idle.unmount();
    const user = render(
      <MessageItem entry={{ ...entry("done"), role: "user", timing: { llm_ms: 900 } } as never} />,
    );
    expect(user.container.querySelector("[data-testid='turn-footer']")).toBeNull();
  });

  it("formats sub-second and second durations the way the ported footer does", () => {
    expect(formatTurnDuration(850)).toBe("850 ms");
    expect(formatTurnDuration(1_542)).toBe("1.5 s");
    expect(formatTurnDuration(0)).toBe("");
    expect(formatTurnDuration(Number.NaN)).toBe("");
  });
});
