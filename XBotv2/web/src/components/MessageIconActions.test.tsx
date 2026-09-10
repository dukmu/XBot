import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { MessageIconActions } from "./MessageIconActions";

describe("MessageIconActions", () => {
  it("copies and regenerates through their explicit owners", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    const regenerate = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText },
    });
    render(<MessageIconActions text="answer" onRegenerate={regenerate} />);

    fireEvent.click(screen.getByRole("button", { name: "Copy" }));
    await waitFor(() => expect(writeText).toHaveBeenCalledWith("answer"));
    fireEvent.click(screen.getByRole("button", { name: "Regenerate response" }));
    await waitFor(() => expect(regenerate).toHaveBeenCalledOnce());
  });

  it("keeps an unavailable branch focusable and explains why", () => {
    const branch = vi.fn().mockResolvedValue(undefined);
    render(
      <MessageIconActions
        text="older answer"
        onBranch={branch}
        branchUnavailable
      />,
    );

    const action = screen.getByRole("button", { name: "Branch into a new conversation" });
    expect(action).toHaveAttribute("aria-disabled", "true");
    expect(action).not.toBeDisabled();
    fireEvent.click(action);
    expect(branch).not.toHaveBeenCalled();
    expect(screen.getByText("Available only on the last message of a completed turn"))
      .toBeInTheDocument();
  });

  it("branches from the completed tail through the supplied session operation", async () => {
    const branch = vi.fn().mockResolvedValue(undefined);
    render(<MessageIconActions text="latest answer" onBranch={branch} />);

    fireEvent.click(screen.getByRole("button", { name: "Branch into a new conversation" }));
    await waitFor(() => expect(branch).toHaveBeenCalledOnce());
  });
});
