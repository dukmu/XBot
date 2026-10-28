import type { ViewEvent } from "../api/types";

export const MAX_ACTIVATION_BUFFERED_EVENTS = 512;

/** Bounded hand-off used while a session baseline is being assembled. */
export class ActivationEventBuffer {
  private events: ViewEvent[] = [];
  private _overflowed = false;

  append(events: readonly ViewEvent[]): void {
    if (this._overflowed) return;
    if (this.events.length + events.length > MAX_ACTIVATION_BUFFERED_EVENTS) {
      this.events = [];
      this._overflowed = true;
      return;
    }
    this.events.push(...events);
  }

  take(): { events: ViewEvent[]; overflowed: boolean } {
    const result = { events: this.events, overflowed: this._overflowed };
    this.events = [];
    this._overflowed = false;
    return result;
  }
}
