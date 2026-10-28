import { afterEach, describe, expect, it, vi } from "vitest";
import { XBotApi } from "./client";
import { projectEvent, projectRecord } from "../state/wireProjection";
import type { ConversationRecord, ServerEvent } from "./wire";

const image = { id: "media/image.png", media_type: "image/png", name: "image.png", kind: "media", size: 5, sha256: "digest" };
const audio = { ...image, id: "media/audio.ogg", media_type: "audio/ogg", name: "audio.ogg" };
afterEach(() => vi.unstubAllGlobals());

describe("media transport", () => {
  it("sends media and its name", async () => {
    const fetcher = vi.fn().mockResolvedValue(new Response(null, { status: 202 }));
    vi.stubGlobal("fetch", fetcher);
    await new XBotApi().sendMessage("s", "t", "", [{ data: "aW1hZ2U=", media_type: "image/png", name: "image.png" }], []);
    expect(JSON.parse(fetcher.mock.calls[0][1].body)).toMatchObject({ media: [{ name: "image.png" }], attachments: [] });
    expect(JSON.parse(fetcher.mock.calls[0][1].body)).not.toHaveProperty("images");
  });

  it("projects canonical history and live tool media", () => {
    const record: ConversationRecord = { kind: "human_input", id: "m", content: "", media: [image, audio], artifacts: [] };
    expect(projectRecord(record).media).toEqual([image, audio]);
    const event: ServerEvent = { protocol_version: "xbotv2.v3", session_id: "s", thread_id: "t", sequence: 1, scope: { kind: "session" }, kind: "tool_completed", payload: { kind: "tool", id: "x", call: { id: "c", name: "read" }, outcome: { kind: "succeeded", output: { parts: [{ kind: "media", artifact: image }], structured: null, artifacts: [] } }, timing: { duration_ms: 1 } } };
    expect(projectEvent(event, (id) => `/download/${id}`).data.outcome).toMatchObject({ output: { parts: [{ artifact: { ...image, url: "/download/media/image.png" } }] } });
  });
});
