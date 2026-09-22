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

/**
 * The settled reply from the ported `math-rendering` scenario, delimiter for
 * delimiter: inline dollar, inline backslash, display backslash, same-line
 * display dollar with a tag, and math inside table cells.
 */
const FIXTURE = [
  "## Math rendering",
  "",
  "Inline dollar $\\theta$ and backslash \\(\\frac{1}{5}\\).",
  "",
  "\\[\\frac{\\pi}{4} < \\theta < \\frac{\\pi}{2}\\]",
  "",
  "$$\\theta \\in \\left(\\frac{\\pi}{4}, \\frac{\\pi}{2}\\right). \\tag{1}$$",
  "",
  "| Symbol | Value |",
  "| --- | --- |",
  "| $\\theta$ | \\(\\frac{1}{5}\\) |",
  "",
  "MATH_RENDERING_DONE",
].join("\n");

const DONE = "MATH_RENDERING_DONE";

/**
 * Math rendering, ported: the scenario asserts six `.katex` roots, two of them
 * display blocks and none of them KaTeX error spans. `mathCompatibility` is the
 * grammar behind the backslash delimiters and the same-line `$$` block, and
 * `rehype-katex` is the renderer, so the DOM the ported UI produces here is the
 * one the scenario snapshot describes.
 */
describe("markdown math", () => {
  it("renders every delimiter of the ported math scenario", () => {
    const { container } = render(<MessageItem entry={assistant(FIXTURE)} />);
    expect(container.textContent).toContain(DONE);
    expect(container.querySelectorAll(".katex")).toHaveLength(6);
    expect(container.querySelectorAll(".katex-display")).toHaveLength(2);
    expect(container.querySelectorAll(".katex-error")).toHaveLength(0);
  });

  it("marks inline TeX as math for assistive technology", () => {
    const { container } = render(<MessageItem entry={assistant(FIXTURE)} />);
    const math = [...container.querySelectorAll("math")];
    // Inline, display and table-cell math all carry the MathML arm, which is
    // what the scenario reads as `math` nodes in the accessibility tree.
    expect(math.length).toBe(6);
  });

  it("leaves ordinary markdown untouched", () => {
    const source = ["# Heading", "", "plain **bold**"].join("\n");
    const { container } = render(<MessageItem entry={assistant(source)} />);
    expect(container.querySelector(".katex")).toBeNull();
    expect(container.querySelector("h1")?.textContent).toBe("Heading");
    expect(container.querySelector("strong")?.textContent).toBe("bold");
  });
});
