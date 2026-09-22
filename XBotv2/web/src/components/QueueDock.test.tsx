import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { PendingInput } from "../api/types";
import { QueueDock, QUEUE_EDIT_UNSUPPORTED, QUEUE_STEER_UNAVAILABLE } from "./QueueDock";

const queued: PendingInput[] = [
  { message_id: "q-1", content: "first queued", target: "next-turn", source: "user", image_count: 0, artifact_count: 0 },
  { message_id: "q-2", content: "second queued", target: "next-turn", source: "user", image_count: 0, artifact_count: 0 },
];

describe("QueueDock", () => {
  it("expands and addresses edit, steer, and remove by stable input id", async () => {
    const update = vi.fn().mockResolvedValue(true);
    render(<QueueDock items={queued} running onUpdate={update} />);

    fireEvent.click(screen.getByRole("button", { name: "2 queued messages" }));
    fireEvent.click(screen.getAllByRole("button", { name: "Edit queued message" })[0]);
    const editor = screen.getByRole("textbox", { name: "Edit queued message" });
    fireEvent.change(editor, { target: { value: "edited queued" } });
    fireEvent.click(screen.getByRole("button", { name: "Save queued message" }));
    await waitFor(() => expect(update).toHaveBeenCalledWith("q-1", {
      action: "edit",
      content: "edited queued",
    }));

    fireEvent.click(screen.getAllByRole("button", { name: "Steer queued message" })[1]);
    await waitFor(() => expect(update).toHaveBeenCalledWith("q-2", { action: "steer" }));
    fireEvent.click(screen.getAllByRole("button", { name: "Remove queued message" })[0]);
    await waitFor(() => expect(update).toHaveBeenCalledWith("q-1", { action: "remove" }));
  });

  it("renders the scenario's collapsed strip: count header and rows only", () => {
    render(<QueueDock items={queued} running onUpdate={vi.fn()} />);
    // The collapsed strip is the count header alone; the rows are hidden until
    // it is expanded, exactly as the queue-actions scenario records it.
    const header = screen.getByRole("button", { name: "2 queued messages" });
    expect(header.getAttribute("aria-expanded")).toBe("false");
    expect(screen.queryByRole("listitem")).toBeNull();
    // No per-delivery-phase chrome: a row is its text plus three actions.
    expect(screen.queryByText("accepted")).toBeNull();
  });

  it("keeps steering items out of the dock and disables an unsupported edit", () => {
    const items: PendingInput[] = [
      { message_id: "q-1", content: "", target: "next-turn", source: "user", image_count: 1, artifact_count: 0 },
      { message_id: "s-1", content: "steer me", target: "next-step", source: "user", image_count: 0, artifact_count: 0 },
    ];
    render(<QueueDock items={items} running onUpdate={vi.fn()} />);
    // One queued row renders directly, without a count header.
    expect(screen.getByText("1 attachment")).toBeTruthy();
    expect(screen.queryByText("steer me")).toBeNull();
    const edit = screen.getByRole("button", { name: "Edit queued message" });
    expect(edit).toHaveProperty("disabled", true);
    expect(edit.getAttribute("title")).toBe(QUEUE_EDIT_UNSUPPORTED);
  });

  it("hints why steering is unavailable while no turn runs", () => {
    render(<QueueDock items={queued} running={false} onUpdate={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "2 queued messages" }));
    const steer = screen.getAllByRole("button", { name: "Steer queued message" })[0];
    expect(steer).toHaveProperty("disabled", true);
    expect(steer.getAttribute("title")).toBe(QUEUE_STEER_UNAVAILABLE);
  });
});
