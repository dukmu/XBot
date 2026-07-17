import { describe, expect, it } from "vitest";
import { decodeSseStream } from "./sse";

describe("decodeSseStream", () => {
  it("decodes UTF-8 events across arbitrary byte chunks", async () => {
    const payload = JSON.stringify({
      protocol_version: "xbotv2.v3",
      session_id: "s",
      thread_id: "agent",
      request_id: "r",
      sequence: 1,
      type: "assistant_message",
      data: { content: "你好" },
    });
    const bytes = new TextEncoder().encode(`event: assistant_message\nid: 1\ndata: ${payload}\n\n`);
    const stream = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(bytes.slice(0, bytes.length - 3));
        controller.enqueue(bytes.slice(bytes.length - 3));
        controller.close();
      },
    });

    const events = [];
    for await (const event of decodeSseStream(stream)) events.push(event);
    expect(events).toHaveLength(1);
    expect(events[0].data.content).toBe("你好");
  });
});
