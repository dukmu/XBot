import { act, render } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { initialRuntimeState, runtimeReducer, type TimelineEntry } from "../state/runtime";
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
        hasNewer={false}
        loadingOlder={false}
        onLoadOlder={async () => {}}
        onLoadLatest={async () => {}}
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
          hasNewer={false}
          loadingOlder={false}
          onLoadOlder={async () => {}}
        onLoadLatest={async () => {}}
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
          hasNewer={false}
          loadingOlder={false}
          onLoadOlder={async () => {}}
        onLoadLatest={async () => {}}
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

describe("Timeline rendering window", () => {
  beforeEach(() => {
    TestResizeObserver.instances = [];
    vi.stubGlobal("ResizeObserver", TestResizeObserver);
    Element.prototype.scrollTo = function scrollTo(this: HTMLElement, options?: ScrollToOptions | number) {
      const top = typeof options === "number" ? options : Number(options?.top || 0);
      Object.defineProperty(this, "scrollTop", { configurable: true, writable: true, value: top });
    };
  });

  it("mounts a bounded window however long the retained transcript is", () => {
    // The reducer caps the retained window at 240 entries; the rendered DOM
    // must stay bounded independently of that, so a long session cannot turn
    // into a long document.
    const entries = Array.from({ length: 10_000 }, (_, index) => message(`entry ${index}`, `entry-${index}`));
    const { container } = renderTimeline(entries);
    const mounted = container.querySelectorAll(".timeline-node").length;
    expect(mounted).toBeGreaterThan(0);
    expect(mounted).toBeLessThanOrEqual(160);
    expect(container.textContent).toContain("entry 9999");

    // Mounting the same content again must not grow the document: the window
    // is a position, not an accumulation.
    renderTimeline(entries);
    expect(container.querySelectorAll(".timeline-node").length).toBe(mounted);
  });
});

describe("Timeline render cost", () => {
  beforeEach(() => {
    TestResizeObserver.instances = [];
    vi.stubGlobal("ResizeObserver", TestResizeObserver);
    Element.prototype.scrollTo = function scrollTo(this: HTMLElement, options?: ScrollToOptions | number) {
      const top = typeof options === "number" ? options : Number(options?.top || 0);
      Object.defineProperty(this, "scrollTop", { configurable: true, writable: true, value: top });
    };
  });

  it("does not cost more per event as the conversation grows", { timeout: 30_000 }, () => {
    const baseline = Array.from({ length: 160 }, (_, index) => ({
      position: index + 1,
      kind: "message" as const,
      message_id: `m-${index + 1}`,
      message: {
        role: "user" as const,
        content: `baseline ${index}`,
        tool_calls: [], tool_call_id: "", status: "", data: null,
        error: null, artifacts: [], images: [],
      },
    }));
    let state = runtimeReducer(initialRuntimeState, { type: "trajectory", items: baseline, nextCursor: null });
    const { container, rerender } = renderTimeline(state.entries);
    const draw = () => rerender(
      <div className="conversation-scroll" data-conversation-scroll>
        <Timeline
          entries={state.entries}
          turnRunning={false}
          onRetry={async () => {}}
          onBranch={async () => {}}
          hasOlder={false}
          hasNewer={false}
          loadingOlder={false}
          onLoadOlder={async () => {}}
          onLoadLatest={async () => {}}
        />
      </div>,
    );

    // Stream events into the reducer and redraw the timeline every 100 events,
    // timing each block.  A window that is a position rather than an
    // accumulation makes the last block cost what the first one did.
    let next = 0;
    const block = (events: number) => {
      const started = performance.now();
      for (let index = 0; index < events; index += 1) {
        next += 1;
        state = runtimeReducer(state, {
          type: "user_message",
          id: `live-${next}`,
          content: `live ${next}`,
          images: [],
        });
        if (index % 500 === 499) act(() => { draw(); });
      }
      return performance.now() - started;
    };

    const first = block(1000);
    block(4_000);
    const last = block(1000);
    act(() => { draw(); });

    expect(container.querySelectorAll(".timeline-node").length).toBeLessThanOrEqual(160);
    expect(state.entries.length).toBeLessThanOrEqual(240);
    expect(container.textContent).toContain(`live ${next}`);
    // Generous factor: the assertion is about growth with history length, not
    // about the machine.  Without the window this block would scan ~21k entries
    // per event and the factor would be far above the bound.
    expect(last).toBeLessThan(Math.max(first, 20) * 4);
  });
});
