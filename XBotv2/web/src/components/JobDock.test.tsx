import { fireEvent, render, screen } from "@testing-library/react";
import { expect, it, vi } from "vitest";
import { JobDock } from "./JobDock";

it("shows the server's job view and stops the live job by id", () => {
  const stop = vi.fn();
  render(<JobDock jobs={[{ id: "job-1", kind: "shell", label: "sleep 45", state: "running", elapsed_ms: 45000, summary: null }]} onStop={stop} onStopAll={vi.fn()} />);
  fireEvent.click(screen.getByRole("button", { name: "1 background job running" }));
  expect(screen.getByText("sleep 45")).toBeTruthy();
  expect(screen.getByText("45s")).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "Stop job-1" }));
  expect(stop).toHaveBeenCalledWith("job-1");
});
