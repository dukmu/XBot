import { describe, expect, it } from "vitest";
import { decodeSseStream } from "./sse";

describe("decodeSseStream", () => {
  it("waits for body cancellation and releases the lock on early return", async () => {
    let releaseCancel!: () => void;
    let cancelStarted!: () => void;
    const started = new Promise<void>((resolve) => { cancelStarted = resolve; });
    const gate = new Promise<void>((resolve) => { releaseCancel = resolve; });
    const body = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(new TextEncoder().encode('data: {"kind":"message","payload":{}}\n\n'));
      },
      cancel() {
        cancelStarted();
        return gate;
      },
    });
    const decoder = decodeSseStream(body);
    expect((await decoder.next()).value.kind).toBe("message");
    let finished = false;
    const returned = decoder.return(undefined).then(() => { finished = true; });
    await started;
    expect(body.locked).toBe(true);
    expect(finished).toBe(false);
    releaseCancel();
    await returned;
    expect(body.locked).toBe(false);
  });

  it("decodes UTF-8 events across arbitrary byte chunks", async () => {
    const payload = JSON.stringify({
      protocol_version: "xbotv2.v3",
      session_id: "s",
      thread_id: "agent",
      scope: { kind: "turn", turn_id: "r" },
      sequence: 1,
      kind: "assistant_text_delta",
      payload: { text: "你好" },
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
    expect(events[0].payload.text).toBe("你好");
  });
});
