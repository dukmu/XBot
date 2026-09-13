import { act, render } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { TimelineEntry } from "../state/runtime";
import { Timeline } from "./Timeline";

class TestResizeObserver {
  static instances: TestResizeObserver[] = [];
  callback: ResizeObserverCallback;

  constructor(callback: ResizeObserverCallback) {
    this.callback = callback;
    TestResizeObserver.instances.push(this);
  }

  observe() {}
  disconnect() {}
  unobserve() {}
}

function message(content: string, id = content): TimelineEntry {
  return {
    id,
    kind: "message",
    role: "assistant",
    content,
    reasoning: "",
    streaming: false,
    images: [],
    messageId: id,
  };
}

/**
 * jsdom has no layout, so the scroll geometry a real browser computes is
 * supplied here.  These tests then assert the follow *decisions* the reducer
 * of the timeline makes from that geometry.
 */
function scrollMetrics(element: HTMLElement, scrollHeight: number, clientHeight: number) {
  let scrollTop = 0;
  Object.defineProperty(element, "scrollHeight", { configurable: true, get: () => scrollHeight });
  Object.defineProperty(element, "clientHeight", { configurable: true, get: () => clientHeight });
  Object.defineProperty(element, "scrollTop", {
    configurable: true,
    get: () => scrollTop,
    set: (value: number) => {
      scrollTop = value;
    },
  });
}

function renderTimeline(entries: TimelineEntry[]) {
  const result = render(
    <div className="conversation-scroll" data-conversation-scroll>
      <Timeline
        entries={entries}
        turnRunning={false}
        onRetry={async () => {}}
        onBranch={async () => {}}
        hasOlder={false}
        loadingOlder={false}
        onLoadOlder={async () => {}}
      />
    </div>,
  );
  const scroller = result.container.querySelector<HTMLElement>("[data-conversation-scroll]")!;
  const content = result.container.querySelector<HTMLElement>(".timeline-inner")!;
  scrollMetrics(scroller, 1000, 300);
  scrollMetrics(content, 1000, 300);
  return { ...result, scroller, content };
}

describe("Timeline follow-latest", () => {
  beforeEach(() => {
    TestResizeObserver.instances = [];
    vi.stubGlobal("ResizeObserver", TestResizeObserver);
    // jsdom has no layout engine: Element.scrollTo does not exist and
    // scrollTop is a plain stored property.
    Element.prototype.scrollTo = function scrollTo(this: HTMLElement, options?: ScrollToOptions | number) {
      const top = typeof options === "number" ? options : Number(options?.top || 0);
      Object.defineProperty(this, "scrollTop", { configurable: true, writable: true, value: top });
    };
  });

  it("pins to the bottom when content only grows below the viewport", () => {
    const { scroller, rerender } = renderTimeline([message("one")]);
    // 700px of content below the fold: a single large step must not cancel
    // following just because the distance exceeds the threshold.
    scrollMetrics(scroller, 1000, 300);
    act(() => scroller.dispatchEvent(new Event("scroll")));

    rerender(
      <div className="conversation-scroll" data-conversation-scroll>
        <Timeline
          entries={[message("one"), message("two")]}
          turnRunning={false}
          onRetry={async () => {}}
          onBranch={async () => {}}
          hasOlder={false}
          loadingOlder={false}
          onLoadOlder={async () => {}}
        />
      </div>,
    );

    expect(scroller.scrollTop).toBe(1000);
  });

  it("stops following after the user scrolls up", () => {
    const { scroller, rerender } = renderTimeline([message("one")]);
    const timeline = scroller.querySelector<HTMLElement>(".timeline")!;
    // Start at the bottom, then leave it the way a user does: wheel up, then
    // the browser reports the resulting scroll position.
    act(() => {
      scroller.scrollTop = 1000;
      scroller.dispatchEvent(new Event("scroll"));
    });
    act(() => timeline.dispatchEvent(new WheelEvent("wheel", { deltaY: -120, bubbles: true })));
    act(() => {
      scroller.scrollTop = 400;
      scroller.dispatchEvent(new Event("scroll"));
    });

    rerender(
      <div className="conversation-scroll" data-conversation-scroll>
        <Timeline
          entries={[message("one"), message("two")]}
          turnRunning={false}
          onRetry={async () => {}}
          onBranch={async () => {}}
          hasOlder={false}
          loadingOlder={false}
          onLoadOlder={async () => {}}
        />
      </div>,
    );

    expect(scroller.scrollTop).toBe(400);
  });

  it("re-pins after late height (markdown, images) lands while following", () => {
    const { scroller, content } = renderTimeline([message("one")]);
    scrollMetrics(content, 2000, 300);

    act(() => TestResizeObserver.instances.forEach((observer) => (
      observer.callback([], observer as unknown as ResizeObserver)
    )));

    expect(scroller.scrollTop).toBe(1000);
  });
});
