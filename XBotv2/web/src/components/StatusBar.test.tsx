import { describe, expect, it } from "vitest";
import { compact } from "./StatusBar";

describe("compact", () => {
  it("formats session token totals", () => {
    expect(compact(999)).toBe("999");
    expect(compact(1_250)).toBe("1.3k");
    expect(compact(1_500_000)).toBe("1.5M");
  });
});
