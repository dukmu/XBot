import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { DiffBlock } from "./DiffBlock";

describe("DiffBlock", () => {
  it("keeps a long middle out of the DOM until expanded", () => {
    const content = Array.from({ length: 20 }, (_, index) => `line-${index}`).join("\n");
    const view = render(<DiffBlock diffs={[{ path: "src/app.py", oldText: null, newText: content }]} maxLines={8} />);

    expect(within(view.container).queryByText("line-10", { exact: true })).toBeNull();
    const expand = screen.getByRole("button", { name: "Show 13 hidden diff lines" });
    expect(expand).toHaveAttribute("aria-expanded", "false");
    fireEvent.click(expand);

    expect(screen.getByText("line-10", { exact: true })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Collapse diff" })).toHaveAttribute("aria-expanded", "true");
  });

  it("copies the complete diff while its visual body is collapsed", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText } });
    const content = Array.from({ length: 20 }, (_, index) => `line-${index}`).join("\n");
    render(<DiffBlock diffs={[{ path: "src/app.py", oldText: "before\n", newText: content }]} maxLines={8} />);

    fireEvent.click(screen.getByRole("button", { name: "Copy diff" }));

    await waitFor(() => expect(writeText).toHaveBeenCalledWith([
      "src/app.py",
      "- before",
      ...Array.from({ length: 20 }, (_, index) => `+ line-${index}`),
    ].join("\n")));
  });
});
