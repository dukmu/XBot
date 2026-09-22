import { fireEvent, render, screen } from "@testing-library/react";
import { beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import { DshAppFrame } from "./DshAppFrame";

class TestResizeObserver {
  observe() {}
  disconnect() {}
}

describe("DshAppFrame", () => {
  beforeEach(() => {
    window.localStorage.clear();
    Object.defineProperty(window, "innerWidth", { configurable: true, value: 1440 });
    vi.stubGlobal("ResizeObserver", TestResizeObserver);
  });

  it("starts expanded when no sidebar preference has been persisted", () => {
    render(
      <DshAppFrame
        mobileSidebarOpen={false}
        sidebar={({ collapsed }) => <span>{collapsed ? "collapsed" : "expanded"}</span>}
      >
        <main>conversation</main>
      </DshAppFrame>,
    );
    expect(screen.getByText("expanded")).toBeVisible();
  });

  it("honors an explicitly persisted collapsed preference", () => {
    window.localStorage.setItem("xbot.sidebar.width", "0");
    render(
      <DshAppFrame
        mobileSidebarOpen={false}
        sidebar={({ collapsed }) => <span>{collapsed ? "collapsed" : "expanded"}</span>}
      >
        <main>conversation</main>
      </DshAppFrame>,
    );
    expect(screen.getByText("collapsed")).toBeVisible();
  });
});

/**
 * Frame drag handles, ported: an 8px hit strip on each column border, pointer
 * capture during the drag, a clamped width remembered across reloads, and the
 * visible pill only on the details handle (`handles.expected.md`: the sidebar
 * keeps only the hit strip).
 *
 * jsdom has no pointer-capture API, so the tests install the two methods the
 * component calls; the pointer events themselves are jsdom's own.
 */
describe("frame handles", () => {
  const captured = new Set<HTMLElement>();

  beforeAll(() => {
    Object.defineProperties(HTMLElement.prototype, {
      setPointerCapture: {
        configurable: true,
        value(this: HTMLElement) { captured.add(this); },
      },
      hasPointerCapture: {
        configurable: true,
        value(this: HTMLElement) { return captured.has(this); },
      },
      releasePointerCapture: {
        configurable: true,
        value(this: HTMLElement) { captured.delete(this); },
      },
    });
  });

  beforeEach(() => {
    captured.clear();
    window.localStorage.clear();
  });

  // jsdom has no PointerEvent, so the pointer fields are assigned onto a plain
  // event; React still sees a `pointerdown`/`pointermove`/`pointerup` and reads
  // clientX from it.
  function pointer(type: string, init: { pointerId: number; clientX: number }) {
    const event = new Event(type, { bubbles: true, cancelable: true });
    Object.assign(event, init);
    return event;
  }

  function frame() {
    return render(
      <DshAppFrame
        mobileSidebarOpen={false}
        sidebar={() => <div />}
        detail={<div />}
      >
        <div />
      </DshAppFrame>,
    );
  }

  it("resizes the sidebar from its hit strip and remembers the width", () => {
    const { container } = frame();
    const [sidebarHandle, detailHandle] = [...container.querySelectorAll("div")].filter((element) => (
      element.className.includes("handle")
    ));
    // The sidebar handle carries no pill side; the details handle does.
    expect(sidebarHandle.getAttribute("data-side")).toBeNull();
    expect(detailHandle.getAttribute("data-side")).toBe("details");

    fireEvent(sidebarHandle, pointer("pointerdown", { pointerId: 1, clientX: 280 }));
    fireEvent(sidebarHandle, pointer("pointermove", { pointerId: 1, clientX: 340 }));
    expect(container.querySelector<HTMLElement>('[class*="frame"]')?.style.gridTemplateColumns)
      .toContain("340px");
    fireEvent(sidebarHandle, pointer("pointerup", { pointerId: 1, clientX: 340 }));
    expect(window.localStorage.getItem("xbot.sidebar.width")).toBe("340");
  });

  it("clamps a sidebar drag to its bounds", () => {
    const { container } = frame();
    const handle = [...container.querySelectorAll("div")].find((element) => element.className.includes("handle"))!;
    fireEvent(handle, pointer("pointerdown", { pointerId: 1, clientX: 280 }));
    fireEvent(handle, pointer("pointermove", { pointerId: 1, clientX: 900 }));
    fireEvent(handle, pointer("pointerup", { pointerId: 1, clientX: 900 }));
    expect(window.localStorage.getItem("xbot.sidebar.width")).toBe("420");
  });

  it("resizes the details column the other way round", () => {
    const { container } = frame();
    const handle = [...container.querySelectorAll("div")]
      .find((element) => element.getAttribute("data-side") === "details")!;
    fireEvent(handle, pointer("pointerdown", { pointerId: 2, clientX: 900 }));
    fireEvent(handle, pointer("pointermove", { pointerId: 2, clientX: 800 }));
    // Dragging left widens the column; the default is 360.
    expect(container.querySelector<HTMLElement>('[class*="frame"]')?.style.gridTemplateColumns)
      .toContain("460px");
    fireEvent(handle, pointer("pointerup", { pointerId: 2, clientX: 800 }));
    expect(window.localStorage.getItem("xbot.details.width")).toBe("460");
  });
});
