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

const DONE = "REMOTE_IMAGE_DONE";
const REMOTE = "http://127.0.0.1:41234/image.png";

/** The ported scenario's fixture: one remote image, one local path. */
const FIXTURE = [
  "## Markdown images",
  "",
  `![Remote test image](${REMOTE})`,
  "",
  "![Local test image](./local-image.png)",
  "",
  DONE,
].join("\n");

/**
 * Markdown image policy, ported: only an absolute `http(s)` source is fetched,
 * so the scenario shows the remote image as an `<img>` and the local path as
 * its alt text. Rendering the local path as an `<img>` would issue a request the
 * page is not allowed to make and show a broken image.
 */
describe("markdown image policy", () => {
  it("loads a remote image and keeps its alt text", () => {
    const { container } = render(<MessageItem entry={assistant(FIXTURE)} />);
    const images = [...container.querySelectorAll("img")];
    expect(images).toHaveLength(1);
    expect(images[0].getAttribute("src")).toBe(REMOTE);
    expect(images[0].getAttribute("alt")).toBe("Remote test image");
    expect(images[0].getAttribute("loading")).toBe("lazy");
  });

  it("renders a local path as alt text instead of an image request", () => {
    const { container } = render(<MessageItem entry={assistant(FIXTURE)} />);
    expect([...container.querySelectorAll("img")].map((image) => image.getAttribute("alt")))
      .not.toContain("Local test image");
    const fallback = [...container.querySelectorAll("span")]
      .filter((element) => element.textContent === "Local test image");
    expect(fallback).toHaveLength(1);
  });

  it("refuses non-fetchable protocols in the same way", () => {
    const content = [
      "![Data image](data:image/png;base64,AAAA)",
      "",
      "![File image](file:///tmp/local.png)",
    ].join("\n");
    const { container } = render(<MessageItem entry={assistant(content)} />);
    expect(container.querySelectorAll("img")).toHaveLength(0);
    expect(container.textContent).toContain("Data image");
    expect(container.textContent).toContain("File image");
  });
});
