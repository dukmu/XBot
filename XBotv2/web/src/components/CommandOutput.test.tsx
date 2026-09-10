import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { CommandOutput } from "./CommandOutput";

describe("CommandOutput", () => {
  it("resets expansion state for each new result", () => {
    const first = { command: "diagnostics", status: "ok" as const, message: Array(17).fill("first").join("\n"), effects: [] };
    const second = { command: "diagnostics", status: "ok" as const, message: Array(17).fill("second").join("\n"), effects: [] };
    const { rerender } = render(<CommandOutput result={first} onClose={() => undefined} />);

    expect(document.querySelector("pre")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /diagnostics/i }));
    expect(document.querySelector("pre")?.textContent).toBe(first.message);

    rerender(<CommandOutput result={second} onClose={() => undefined} />);
    expect(document.querySelector("pre")).not.toBeInTheDocument();
  });
});
