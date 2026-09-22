import { fireEvent, render } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { GOAL_SLOTS, GoalBar, goalPhaseLabel } from "./GoalBar";
import type { RuntimeSession } from "../state/runtime";

function session(slots: Record<string, string>): RuntimeSession {
  return { title: "s", workspace_root: "/w", status_slots: slots } as unknown as RuntimeSession;
}

const handlers = { onPause: () => undefined, onEdit: () => undefined, onClear: () => undefined };

describe("goalPhaseLabel", () => {
  it("names each phase the ported way", () => {
    expect(goalPhaseLabel("active")).toBe("Ongoing Goal");
    expect(goalPhaseLabel("paused")).toBe("Goal paused");
    expect(goalPhaseLabel("achieved")).toBe("Goal achieved");
    expect(goalPhaseLabel(undefined)).toBeNull();
  });
});

describe("GoalBar", () => {
  it("shows the objective and its round progress", () => {
    const { getByText } = render(
      <GoalBar
        current={session({
          [GOAL_SLOTS.status]: "active",
          [GOAL_SLOTS.objective]: "ship the API",
          [GOAL_SLOTS.round]: "2/20",
        })}
        {...handlers}
      />,
    );
    expect(getByText("Ongoing Goal")).toBeTruthy();
    expect(getByText("ship the API")).toBeTruthy();
    expect(getByText("Rounds: 2/20")).toBeTruthy();
  });

  it("pauses, edits and clears through its actions", () => {
    const onPause = vi.fn();
    const onEdit = vi.fn();
    const onClear = vi.fn();
    const { getByLabelText } = render(
      <GoalBar
        current={session({ [GOAL_SLOTS.status]: "active", [GOAL_SLOTS.objective]: "x" })}
        onPause={onPause}
        onEdit={onEdit}
        onClear={onClear}
      />,
    );
    fireEvent.click(getByLabelText("Pause goal"));
    fireEvent.click(getByLabelText("Edit goal"));
    fireEvent.click(getByLabelText("Clear goal"));
    expect(onPause).toHaveBeenCalledTimes(1);
    expect(onEdit).toHaveBeenCalledTimes(1);
    expect(onClear).toHaveBeenCalledTimes(1);
  });

  it("renders nothing without a goal, and locks pause once it is over", () => {
    const { container } = render(<GoalBar current={session({})} {...handlers} />);
    expect(container.querySelector(".goal-bar")).toBeNull();

    const { getByLabelText, getByText } = render(
      <GoalBar
        current={session({ [GOAL_SLOTS.status]: "achieved", [GOAL_SLOTS.stats]: "1 turn" })}
        {...handlers}
      />,
    );
    expect(getByText("Goal achieved")).toBeTruthy();
    expect((getByLabelText("Pause goal") as HTMLButtonElement).disabled).toBe(true);
  });
});
