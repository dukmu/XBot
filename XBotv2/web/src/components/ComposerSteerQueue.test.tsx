import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { EMPTY_USAGE } from "../api/types";
import { Composer, COMPOSER_PLACEHOLDER, STEER_QUEUE_PLACEHOLDER } from "./Composer";

function composer(overrides: Partial<Parameters<typeof Composer>[0]> = {}) {
  const onSend = vi.fn().mockResolvedValue(true);
  const onSteerAll = vi.fn();
  const view = render(
    <Composer
      running
      disabled={false}
      commands={[]}
      draft={null}
      allowImages
      usage={EMPTY_USAGE}
      contextWindow={128000}
      onSend={onSend}
      inputHistory={[]}
      onSubmitted={vi.fn()}
      onInterrupt={vi.fn().mockResolvedValue(undefined)}
      pendingCount={2}
      onSteerAll={onSteerAll}
      {...overrides}
    />,
  );
  return { onSend, onSteerAll, view, textarea: screen.getByRole("textbox", { name: "Message XBot" }) };
}

/**
 * The ported whole-queue gesture: while a turn runs and the draft is empty, the
 * composer advertises that `Cmd/Ctrl+Enter` steers every queued message, and the
 * chord steers instead of submitting the empty draft.
 */
describe("composer steer-queue gesture", () => {
  it("advertises the whole-queue steer hint only while it is actionable", () => {
    const { textarea, view } = composer();
    expect(textarea.getAttribute("placeholder")).toBe(STEER_QUEUE_PLACEHOLDER);
    view.unmount();

    // Nothing queued: the hint would name a no-op.
    const idle = composer({ pendingCount: 0 });
    expect(idle.textarea.getAttribute("placeholder")).toBe(COMPOSER_PLACEHOLDER);
    idle.view.unmount();

    // A draft owns the chord again.
    const drafting = composer();
    fireEvent.change(drafting.textarea, { target: { value: "typed" } });
    expect(drafting.textarea.getAttribute("placeholder")).toBe(COMPOSER_PLACEHOLDER);
  });

  it("steers all queued messages on Cmd/Ctrl+Enter instead of sending", () => {
    const { onSend, onSteerAll, textarea } = composer();
    fireEvent.keyDown(textarea, { key: "Enter", ctrlKey: true });
    expect(onSteerAll).toHaveBeenCalledTimes(1);
    expect(onSend).not.toHaveBeenCalled();

    // A draft takes the chord back: accelerated Enter submits it.
    fireEvent.change(textarea, { target: { value: "hello" } });
    fireEvent.keyDown(textarea, { key: "Enter", metaKey: true });
    expect(onSteerAll).toHaveBeenCalledTimes(1);
    expect(onSend).toHaveBeenCalledWith("hello", []);
  });
});

/**
 * The ported draft geometry: one scrollport, a hidden mirror that carries the
 * full draft in normal flow (the height authority), and the textarea riding it —
 * never a scroller of its own.
 */
describe("composer draft stack", () => {
  it("puts the textarea inside the scrollport beside its mirror", () => {
    const { textarea } = composer();
    const scrollport = textarea.closest(".composer-draft-scroll");
    expect(scrollport).not.toBeNull();
    const grow = scrollport!.querySelector(".composer-draft-grow");
    const mirror = grow?.querySelector("[data-input-mirror]");
    expect(mirror).not.toBeNull();
    // The mirror is the height authority and stays out of the accessibility tree.
    expect(mirror?.getAttribute("aria-hidden")).toBe("true");
    expect(grow?.contains(textarea)).toBe(true);
    expect(mirror?.textContent).toBe("\n");
  });

  it("keeps the mirror in step with the draft", () => {
    const { textarea } = composer();
    fireEvent.change(textarea, { target: { value: "first line\nsecond line" } });
    expect(document.querySelector("[data-input-mirror]")?.textContent).toBe("first line\nsecond line\n");
  });
});
