import { beforeEach, describe, expect, it, vi } from "vitest";
import { clearOpenSession, readOpenSession, writeOpenSession } from "./openSession";

describe("open session persistence", () => {
  beforeEach(() => {
    window.localStorage.clear();
    vi.restoreAllMocks();
  });

  it("round-trips the session a browser had open", () => {
    writeOpenSession({ session_id: "s1", thread_id: "agent", workspace_root: "/workspace" });

    expect(readOpenSession()).toEqual({
      session_id: "s1",
      thread_id: "agent",
      workspace_root: "/workspace",
    });
  });

  it("ignores absent, malformed, or incomplete entries", () => {
    expect(readOpenSession()).toBeNull();

    window.localStorage.setItem("xbotv2.openSession", "{not json");
    expect(readOpenSession()).toBeNull();

    window.localStorage.setItem("xbotv2.openSession", JSON.stringify({ session_id: "s1" }));
    expect(readOpenSession()).toBeNull();

    window.localStorage.setItem("xbotv2.openSession", JSON.stringify([]));
    expect(readOpenSession()).toBeNull();
  });

  it("clears the stored session", () => {
    writeOpenSession({ session_id: "s1", thread_id: "agent", workspace_root: "" });
    clearOpenSession();

    expect(readOpenSession()).toBeNull();
  });

  it("survives a storage backend that refuses access", () => {
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("storage denied");
    });
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("storage denied");
    });
    vi.spyOn(Storage.prototype, "removeItem").mockImplementation(() => {
      throw new Error("storage denied");
    });

    expect(readOpenSession()).toBeNull();
    expect(() => writeOpenSession({
      session_id: "s1",
      thread_id: "agent",
      workspace_root: "",
    })).not.toThrow();
    expect(() => clearOpenSession()).not.toThrow();
  });
});
