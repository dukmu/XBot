import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { ContextInjectionRow } from "./ContextInjectionRow";

describe("ContextInjectionRow", () => {
  it("shows lifecycle activity directly instead of an empty disclosure", () => {
    render(
      <ContextInjectionRow
        entry={{
          id: "event:4:turn_started",
          kind: "runtime",
          source: "turn",
          event: "turn_started",
          content: "Turn 2 started",
        }}
      />,
    );

    expect(screen.getByRole("status")).toHaveTextContent("Turn 2 started");
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
