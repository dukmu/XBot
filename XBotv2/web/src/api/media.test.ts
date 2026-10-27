import { afterEach, describe, expect, it, vi } from "vitest";
import { XBotApi } from "./client";
import { historyEntries, initialRuntimeState, runtimeReducer } from "../state/runtime";

const image = { id: "media/image.png", media_type: "image/png", name: "image.png", kind: "media", size: 5, sha256: "digest" };
const audio = { ...image, id: "media/audio.ogg", media_type: "audio/ogg", name: "audio.ogg" };

afterEach(() => vi.unstubAllGlobals());

describe("media mapping", () => {
  it("sends media on the wire and preserves its name", async () => {
    const fetcher = vi.fn().mockResolvedValue(new Response(null, { status: 202 }));
    vi.stubGlobal("fetch", fetcher);
    await new XBotApi().sendMessage("s", "t", "", [{ data: "aW1hZ2U=", media_type: "image/png", name: "image.png" }], []);
    expect(JSON.parse(fetcher.mock.calls[0][1].body)).toMatchObject({ media: [{ name: "image.png" }], attachments: [] });
    expect(JSON.parse(fetcher.mock.calls[0][1].body)).not.toHaveProperty("images");
  });

  it("maps history and tool media parts to artifact URLs", async () => {
    const base = { content: "", tool_calls: [], tool_call_id: "", status: "", data: null, error: null, artifacts: [] };
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({ messages: [
      { ...base, role: "user", media: [image, audio] },
      { ...base, role: "tool", tool_call_id: "call", outcome: { kind: "succeeded", output: { parts: [{ kind: "media", artifact: image }] } } },
    ] }), { status: 200 })));
    const page = await new XBotApi().listMessages("s", "t");
    const entries = historyEntries(page.messages);
    expect(entries[0]).toMatchObject({ images: [
      { src: "/api/sessions/s/threads/t/artifacts/media/image.png" },
      { href: "/api/sessions/s/threads/t/artifacts/media/audio.ogg", src: undefined },
    ] });
    expect(entries[1]).toMatchObject({ images: [{ ...image, url: "/api/sessions/s/threads/t/artifacts/media/image.png" }] });
  });

  it("decorates live notice and tool media references", async () => {
    const event = (type: string, data: object) => ({ protocol_version: "xbotv2.v3", session_id: "s", thread_id: "t", request_id: "r", sequence: 1, type, data });
    const frames = [event("message", { media: [audio] }), event("tool_result", { outcome: { kind: "succeeded", output: { parts: [{ kind: "media", artifact: image }] } } }), event("end", {})]
      .map((value) => `data: ${JSON.stringify(value)}\n\n`).join("");
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(frames, { status: 200 })));
    const events = [];
    for await (const value of new XBotApi().streamEvents("s", "t", 0)) events.push(value);
    expect(events[0].data.media).toEqual([{ ...audio, url: "/api/sessions/s/threads/t/artifacts/media/audio.ogg" }]);
    expect(events[1].data.outcome).toMatchObject({ output: { parts: [{ artifact: { ...image, url: "/api/sessions/s/threads/t/artifacts/media/image.png" } }] } });
  });

  it("keeps runtime notice media and queue counts", () => {
    const state = runtimeReducer(initialRuntimeState, { type: "events", events: [
      { protocol_version: "xbotv2.v3", session_id: "s", thread_id: "t", request_id: "r", sequence: 1, type: "message", data: { id: "m", role: "user", content: "notice", runtime: { source: "system", event: "notice" }, media: [{ ...audio, url: "/audio" }] } },
      { protocol_version: "xbotv2.v3", session_id: "s", thread_id: "t", request_id: "r", sequence: 2, type: "queue_updated", data: { items: [{ message_id: "q", content: "", target: "next-turn", media_count: 1, artifact_count: 0 }] } },
    ] });
    expect(state.entries[0]).toMatchObject({ images: [{ href: "/audio", src: undefined }] });
    expect(state.pendingInputs[0]).toMatchObject({ media_count: 1 });
  });
});
