import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { ContextInjectionRow } from "./ContextInjectionRow";

describe("ContextInjectionRow", () => {
  it.each([
    ["turn_started", "Turn 2 started"],
    ["turn_finished", "Turn 2 finished"],
    ["turn_cancelled", "Turn 2 cancelled"],
  ])("shows %s as a readable lifecycle status", (event, content) => {
    render(
      <ContextInjectionRow
        entry={{
          id: "event:4:turn_started",
          kind: "runtime",
          source: "turn",
          event,
          content,
        }}
      />,
    );

    expect(screen.getByRole("status")).toHaveTextContent(content);
    expect(screen.getByRole("status")).not.toHaveTextContent(event);
    expect(screen.getByRole("status")).toHaveAttribute("title", event);
    expect(screen.queryByText("Context injection")).not.toBeInTheDocument();
  });

  it("keeps injected context expandable with its provenance", () => {
    render(
      <ContextInjectionRow
        entry={{
          id: "runtime:context-1",
          kind: "runtime",
          source: "workspace",
          event: "instructions",
          content: "Use the workspace rules.",
        }}
      />,
    );

    // The accessible name is dsh's: title, producer, detail.
    expect(screen.getByText("Context injection")).toBeVisible();
    const summary = screen.getByText("Context injection").closest("summary");
    expect(summary?.textContent).toBe("Context injectionworkspaceinstructions");
  });
});
