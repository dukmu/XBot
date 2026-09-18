import { describe, expect, it } from "vitest";
import type { ServerEvent } from "../api/types";
import {
  ActivationEventBuffer,
  MAX_ACTIVATION_BUFFERED_EVENTS,
} from "./activationBuffer";
import { initialRuntimeState, runtimeReducer } from "./runtime";

function event(type: ServerEvent["type"], sequence: number): ServerEvent {
  return { type, sequence, data: {} } as ServerEvent;
}

describe("ActivationEventBuffer", () => {
  it("keeps subscription frames ordered until the baseline folds them", () => {
    const buffer = new ActivationEventBuffer();
    buffer.append([
      event("turn_started", 11),
      event("assistant_message", 12),
      event("turn_finished", 13),
    ]);

    const drained = buffer.take();
    expect(drained.overflowed).toBe(false);
    expect(drained.events.map((item) => item.sequence)).toEqual([11, 12, 13]);

    const state = runtimeReducer(initialRuntimeState, {
      type: "trajectory",
      items: [],
      nextCursor: null,
      bufferedEvents: drained.events,
    });
    expect(state.turnRunning).toBe(false);
  });

  it("reports overflow and can be reset for a fresh baseline", () => {
    const buffer = new ActivationEventBuffer();
    buffer.append(Array.from(
      { length: MAX_ACTIVATION_BUFFERED_EVENTS },
      (_, index) => event("usage", index + 1),
    ));
    buffer.append([event("turn_finished", MAX_ACTIVATION_BUFFERED_EVENTS + 1)]);

    expect(buffer.take()).toEqual({ events: [], overflowed: true });
    buffer.append([event("turn_finished", 1)]);
    expect(buffer.take().events.map((item) => item.sequence)).toEqual([1]);
  });
});
