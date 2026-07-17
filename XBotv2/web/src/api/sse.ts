import type { ServerEvent } from "./types";

interface SseFrame {
  event: string;
  data: string;
  id: string;
}

export async function* decodeSseStream(
  body: ReadableStream<Uint8Array>,
): AsyncGenerator<ServerEvent> {
  const reader = body.getReader();
  const decoder = new TextDecoder("utf-8", { fatal: true });
  let buffer = "";
  let frame: SseFrame = emptyFrame();

  const feedLine = (line: string): ServerEvent | null => {
    if (line === "") {
      if (!frame.data) return null;
      const event = parseFrame(frame);
      frame = emptyFrame();
      return event;
    }
    if (line.startsWith(":")) return null;
    const split = line.indexOf(":");
    const name = split < 0 ? line : line.slice(0, split);
    let value = split < 0 ? "" : line.slice(split + 1);
    if (value.startsWith(" ")) value = value.slice(1);
    if (name === "event") frame.event = value;
    if (name === "id") frame.id = value;
    if (name === "data") frame.data += `${frame.data ? "\n" : ""}${value}`;
    return null;
  };

  try {
    while (true) {
      const { done, value } = await reader.read();
      buffer += decoder.decode(value, { stream: !done });
      const lines = buffer.split(/\r?\n/);
      buffer = done ? "" : (lines.pop() ?? "");
      for (const line of lines) {
        const event = feedLine(line);
        if (event) yield event;
      }
      if (done) {
        if (buffer) {
          const event = feedLine(buffer);
          if (event) yield event;
        }
        if (frame.data) yield parseFrame(frame);
        return;
      }
    }
  } finally {
    reader.releaseLock();
  }
}

function emptyFrame(): SseFrame {
  return { event: "", data: "", id: "" };
}

function parseFrame(frame: SseFrame): ServerEvent {
  const parsed = JSON.parse(frame.data) as ServerEvent;
  if (!parsed || typeof parsed !== "object" || typeof parsed.type !== "string") {
    throw new Error("Invalid XBot SSE event");
  }
  return parsed;
}
