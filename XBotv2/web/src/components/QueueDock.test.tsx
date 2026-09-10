import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { PendingInput } from "../api/types";
import { QueueDock } from "./QueueDock";

const queued: PendingInput[] = [
  { message_id: "q-1", content: "first queued", target: "next-turn", source: "user", image_count: 0, artifact_count: 0 },
  { message_id: "q-2", content: "second queued", target: "next-turn", source: "user", image_count: 0, artifact_count: 0 },
];

describe("QueueDock", () => {
  it("expands and addresses edit, steer, and remove by stable input id", async () => {
    const update = vi.fn().mockResolvedValue(true);
    render(<QueueDock items={queued} running onUpdate={update} />);

    fireEvent.click(screen.getByRole("button", { name: "2 queued" }));
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
});
