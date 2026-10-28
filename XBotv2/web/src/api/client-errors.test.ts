import { describe, expect, it } from "vitest";
import { XBotApiError, isMissingSessionError } from "./client";

describe("isMissingSessionError", () => {
  it("recognises a session or thread that no longer exists", () => {
    expect(isMissingSessionError(new XBotApiError(404, "not_found", "no such session"))).toBe(true);
    expect(isMissingSessionError(new XBotApiError(400, "not_found", "gone"))).toBe(true);
  });

  it("leaves real failures to the caller", () => {
    // A 500 or a rejected cursor is not "the session is gone": the first must
    // be reported, the second has its own re-anchor path.
    expect(isMissingSessionError(new XBotApiError(500, "internal_error", "boom"))).toBe(false);
    expect(isMissingSessionError(new XBotApiError(400, "invalid_cursor", "stale"))).toBe(false);
    expect(isMissingSessionError(new Error("offline"))).toBe(false);
    expect(isMissingSessionError(undefined)).toBe(false);
  });
});
