"""JSONL protocol frames — ported from XBot v1.

Protocol boundary: only this module may translate runtime events to
UI-facing wire payloads. TUI clients consume ProtocolFrame objects
and must not import provider SDKs, tool execution, or runtime internals.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field

PROTOCOL_VERSION = "xbotv2.v1"
Direction = Literal["client_to_server", "server_to_client"]


class ProtocolFrame(BaseModel):
    """Wire envelope for every JSONL command/event.

    Public fields:
    - protocol_version, frame_id, seq, ts: envelope metadata.
    - direction, type: routing metadata.
    - session_id, thread_id, request_id: correlation IDs.
    - payload: JSON-serializable body.
    """

    protocol_version: str = PROTOCOL_VERSION
    frame_id: str = Field(default_factory=lambda: f"frame_{uuid.uuid4().hex}")
    seq: int
    ts: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    direction: Direction
    type: str
    session_id: str
    thread_id: str
    request_id: str
    payload: dict[str, Any] = Field(default_factory=dict)

    def to_json_line(self) -> str:
        return self.model_dump_json(exclude_none=True) + "\n"


def frame_from_json(line: str) -> ProtocolFrame:
    frame = ProtocolFrame.model_validate_json(line)
    if frame.protocol_version != PROTOCOL_VERSION:
        raise ValueError(
            f"Unsupported protocol version {frame.protocol_version!r}; "
            f"expected {PROTOCOL_VERSION!r}"
        )
    return frame


class ProtocolEncoder:
    """Encode internal runtime events into UI protocol frames.

    Only this class maps internal event types to wire frame types.
    TUI/terminal clients never see internal event objects.
    """

    def __init__(
        self,
        session_id: str,
        thread_id: str,
        direction: Direction = "server_to_client",
    ) -> None:
        self._session_id = session_id
        self._thread_id = thread_id
        self._direction = direction
        self._seq = 0
        self._usage_total = {
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "requests": 0,
        }

    def encode(
        self,
        event_type: str,
        payload: dict[str, Any] | None = None,
        request_id: str = "",
    ) -> ProtocolFrame:
        self._seq += 1
        return ProtocolFrame(
            seq=self._seq,
            direction=self._direction,
            type=event_type,
            session_id=self._session_id,
            thread_id=self._thread_id,
            request_id=request_id or (payload.get("request_id", "") if payload else ""),
            payload=payload or {},
        )

    def encode_turn_started(self, turn: int, request_id: str = "") -> ProtocolFrame:
        return self.encode("turn_started", {"turn": turn}, request_id=request_id)

    def encode_turn_finished(self, turn: int, request_id: str = "") -> ProtocolFrame:
        return self.encode("turn_finished", {"turn": turn}, request_id=request_id)

    def encode_assistant_message(
        self, content: str, tool_calls: list | None = None, request_id: str = ""
    ) -> ProtocolFrame:
        return self.encode("assistant_message", {
            "content": content,
            "tool_calls": tool_calls,
        }, request_id=request_id)

    def encode_tool_calls_started(
        self, tool_calls: list[dict], request_id: str = ""
    ) -> ProtocolFrame:
        return self.encode("tool_calls_started", {"tool_calls": tool_calls}, request_id=request_id)

    def encode_tool_result(
        self,
        tool_call_id: str,
        content: str,
        status: str = "success",
        request_id: str = "",
    ) -> ProtocolFrame:
        return self.encode("tool_result", {
            "tool_call_id": tool_call_id,
            "content": content,
            "status": status,
        }, request_id=request_id)

    def encode_usage(
        self,
        usage: dict[str, Any],
        request_id: str = "",
    ) -> ProtocolFrame:
        delta = _normalize_usage(usage)
        for key in self._usage_total:
            self._usage_total[key] += delta.get(key, 0)
        return self.encode(
            "usage",
            {
                "delta": delta,
                "total": dict(self._usage_total),
            },
            request_id=request_id,
        )

    def encode_error(
        self, message: str, code: str = "runtime_error", request_id: str = ""
    ) -> ProtocolFrame:
        return self.encode("error", {"message": message, "code": code}, request_id=request_id)

    def encode_status(self, text: str, request_id: str = "") -> ProtocolFrame:
        return self.encode("status", {"text": text}, request_id=request_id)

    def encode_session_ready(
        self, agent_name: str = "XBotv2", request_id: str = ""
    ) -> ProtocolFrame:
        return self.encode("session_ready", {"agent_name": agent_name}, request_id=request_id)

    def encode_hello_ok(
        self, server_name: str = "xbotv2", request_id: str = ""
    ) -> ProtocolFrame:
        return self.encode("hello_ok", {"server_name": server_name}, request_id=request_id)

    def encode_shutdown_ok(self, request_id: str = "") -> ProtocolFrame:
        return self.encode("shutdown_ok", {}, request_id=request_id)


def _normalize_usage(value: dict[str, Any] | None) -> dict[str, int]:
    if not isinstance(value, dict):
        return {
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "requests": 0,
        }
    input_tokens = int(value.get("input_tokens") or value.get("prompt_tokens") or 0)
    output_tokens = int(value.get("output_tokens") or value.get("completion_tokens") or 0)
    total_tokens = int(value.get("total_tokens") or input_tokens + output_tokens)
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
        "requests": int(value.get("requests") or 1),
    }
