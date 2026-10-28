import { fireEvent, render, screen } from "@testing-library/react";
import { expect, it, vi } from "vitest";
import { ThreadPageControls } from "./ThreadPageControls";

it("uses opaque cursors to enable only available page navigation", () => {
  const page = vi.fn();
  const { rerender } = render(<ThreadPageControls previousCursor={null} nextCursor="next" loading={false} onPage={page} />);
  expect(screen.getByText("Thread list snapshot")).toBeVisible();
  expect(screen.getByRole("button", { name: "Previous threads" })).toBeDisabled();
  fireEvent.click(screen.getByRole("button", { name: "Next threads" }));
  expect(page).toHaveBeenCalledWith("next");
  rerender(<ThreadPageControls previousCursor="prev" nextCursor={null} loading={false} onPage={page} />);
  expect(screen.getByRole("button", { name: "Next threads" })).toBeDisabled();
  fireEvent.click(screen.getByRole("button", { name: "Previous threads" }));
  expect(page).toHaveBeenCalledWith("previous");
});
