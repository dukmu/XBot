import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { ToolOutput } from "./ToolOutput";

describe("ToolOutput", () => {
  it("keeps the middle of long output out of the DOM until expanded", () => {
    const lines = Array.from({ length: 40 }, (_, index) => `line-${index}`);
    render(<ToolOutput value={lines.join("\n")} label="Result" />);

    expect(screen.queryByText("line-20", { exact: false })).toBeNull();
    const expand = screen.getByRole("button", { name: "Show 24 hidden lines" });
    expect(expand).toHaveAttribute("aria-expanded", "false");
    fireEvent.click(expand);

    expect(screen.getByText("line-20", { exact: false })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Collapse output" })).toHaveAttribute("aria-expanded", "true");
  });

  it("copies the complete output while the visual preview remains capped", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText } });
    const output = `${"a".repeat(12_000)}middle${"z".repeat(12_000)}`;
    const view = render(<ToolOutput value={output} />);

    expect(within(view.container).queryByText("middle", { exact: false })).toBeNull();
    fireEvent.click(within(view.container).getByRole("button", { name: "Copy" }));

    await waitFor(() => expect(writeText).toHaveBeenCalledWith(output));
  });
});
