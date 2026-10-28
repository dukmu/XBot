import { render } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { MessageItem } from "./MessageItem";
import type { TimelineEntry } from "../state/runtime";

type MessageEntry = Extract<TimelineEntry, { kind: "message" }>;

function assistant(content: string): MessageEntry {
  return {
    id: "m1",
    kind: "message",
    role: "assistant",
    content,
    reasoning: "",
    streaming: false,
    messageId: "m1",
    images: [],
  } as unknown as MessageEntry;
}

const DONE = "CJK_STRONG_DONE";

/** One `[markdown, strong text, whole paragraph]` case per fixture block. */
const CASES = [
  ["**注意：**内容", "注意：", "注意：内容"],
  ["**Notice:**内容", "Notice:", "Notice:内容"],
  ["**事件中间件（waterfall）**实现", "事件中间件（waterfall）", "事件中间件（waterfall）实现"],
  ["**事件中间件(waterfall)**实现", "事件中间件(waterfall)", "事件中间件(waterfall)实现"],
  ["**句号。**后续", "句号。", "句号。后续"],
  ["**Period.**后续", "Period.", "Period.后续"],
  ["**提醒！**继续", "提醒！", "提醒！继续"],
  ["**Warning!**继续", "Warning!", "Warning!继续"],
] as const;

/** The ported scenario's fixture, built as dsh builds it: one block per case. */
const FIXTURE = [
  "## CJK strong emphasis",
  "",
  ...CASES.flatMap(([markdown]) => [markdown, ""]),
  DONE,
].join("\n");

/**
 * CJK-adjacent strong emphasis, ported: CommonMark will not close `**` after
 * punctuation when CJK prose continues without whitespace, so the ported
 * grammar registers dsh's own attention construct. The scenario is the
 * checklist — every case must come out as one `<strong>` plus its tail text,
 * one paragraph per case.
 */
describe("markdown CJK strong emphasis", () => {
  it("closes strong emphasis before adjacent CJK prose without whitespace", () => {
    const { container } = render(<MessageItem entry={assistant(FIXTURE)} />);
    expect(container.textContent).toContain(DONE);
    const strong = [...container.querySelectorAll("strong")];
    expect(strong.map((element) => element.textContent)).toEqual(CASES.map(([, text]) => text));
  });

  it("leaves one paragraph per case: strong run followed by its tail", () => {
    const { container } = render(<MessageItem entry={assistant(FIXTURE)} />);
    const paragraphs = [...container.querySelectorAll("p")]
      .map((element) => element.textContent)
      .filter((text) => text !== DONE);
    expect(paragraphs).toEqual(CASES.map(([, , paragraph]) => paragraph));
  });
});
