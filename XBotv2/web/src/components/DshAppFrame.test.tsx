import { render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
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
