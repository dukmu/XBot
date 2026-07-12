"""Shared Server-Sent Events wire encoding and incremental decoding."""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from pydantic import ValidationError

from xbotv2.protocol.models import ServerEvent, server_event


@dataclass(frozen=True, slots=True)
class SseMessage:
    event: str | None
    data: str
    event_id: str | None


@dataclass(slots=True)
class SseDecoder:
    """Incrementally decode SSE lines into complete messages."""

    _event: str | None = None
    _data_lines: list[str] = field(default_factory=list)
    _event_id: str | None = None

    def feed(self, line: str) -> SseMessage | None:
        if line == "":
            return self._dispatch()
        if line.startswith(":"):
            return None

        field_name, separator, value = line.partition(":")
        if not separator:
            value = ""
        elif value.startswith(" "):
            value = value[1:]

        if field_name == "event":
            self._event = value
        elif field_name == "data":
            self._data_lines.append(value)
        elif field_name == "id" and "\x00" not in value:
            self._event_id = value
        return None

    def finish(self) -> SseMessage | None:
        """Dispatch a final unterminated message when the stream closes."""
        return self._dispatch()

    def _dispatch(self) -> SseMessage | None:
        if not self._data_lines:
            self._event = None
            self._event_id = None
            return None
        message = SseMessage(
            event=self._event,
            data="\n".join(self._data_lines),
            event_id=self._event_id,
        )
        self._event = None
        self._data_lines.clear()
        self._event_id = None
        return message


def encode_server_event(event: ServerEvent) -> bytes:
    """Encode one validated server event as an SSE message."""
    event_name = _single_line("event type", event.type)
    event_id = _single_line("event id", str(event.sequence))
    payload = json.dumps(event.model_dump(), ensure_ascii=False, default=str)
    data = "".join(f"data: {line}\n" for line in payload.splitlines() or [""])
    return f"event: {event_name}\nid: {event_id}\n{data}\n".encode("utf-8")


def decode_server_event(message: SseMessage) -> ServerEvent:
    """Validate one SSE data payload, surfacing malformed JSON as an event."""
    try:
        payload = json.loads(message.data)
    except json.JSONDecodeError:
        return server_event(
            type="error",
            data={"code": "sse_decode_error", "message": message.data},
        )
    try:
        return ServerEvent.model_validate(payload)
    except ValidationError as exc:
        return server_event(
            type="error",
            data={"code": "sse_decode_error", "message": str(exc)},
        )


def _single_line(label: str, value: str) -> str:
    if "\n" in value or "\r" in value:
        raise ValueError(f"SSE {label} must be a single line")
    return value


__all__ = [
    "SseDecoder",
    "SseMessage",
    "decode_server_event",
    "encode_server_event",
]
