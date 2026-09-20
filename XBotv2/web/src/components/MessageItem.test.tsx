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

import { MessageItem } from "./MessageItem";
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
