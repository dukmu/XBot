"""JSONL protocol frames — ported from XBot v1.

Protocol boundary: only this module may translate runtime events to
UI-facing wire payloads. TUI clients consume ProtocolFrame objects
and must not import LangChain, LangGraph, or runtime internals.
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
    return ProtocolFrame.model_validate_json(line)


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

    def encode(self, event_type: str, payload: dict[str, Any] | None = None) -> ProtocolFrame:
        self._seq += 1
        return ProtocolFrame(
            seq=self._seq,
            direction=self._direction,
            type=event_type,
            session_id=self._session_id,
            thread_id=self._thread_id,
            request_id=payload.get("request_id", "") if payload else "",
            payload=payload or {},
        )

    def encode_turn_started(self, turn: int) -> ProtocolFrame:
        return self.encode("turn_started", {"turn": turn})

    def encode_turn_finished(self, turn: int) -> ProtocolFrame:
        return self.encode("turn_finished", {"turn": turn})

    def encode_assistant_message(
        self, content: str, tool_calls: list | None = None
    ) -> ProtocolFrame:
        return self.encode("assistant_message", {
            "content": content,
            "tool_calls": tool_calls,
        })

    def encode_tool_calls_started(self, tool_calls: list[dict]) -> ProtocolFrame:
        return self.encode("tool_calls_started", {"tool_calls": tool_calls})

    def encode_tool_result(
        self, tool_call_id: str, content: str, status: str = "success"
    ) -> ProtocolFrame:
        return self.encode("tool_result", {
            "tool_call_id": tool_call_id,
            "content": content,
            "status": status,
        })

    def encode_error(self, message: str, code: str = "runtime_error") -> ProtocolFrame:
        return self.encode("error", {"message": message, "code": code})

    def encode_status(self, text: str) -> ProtocolFrame:
        return self.encode("status", {"text": text})

    def encode_session_ready(self, agent_name: str = "XBotv2") -> ProtocolFrame:
        return self.encode("session_ready", {"agent_name": agent_name})

    def encode_hello_ok(self, server_name: str = "xbotv2") -> ProtocolFrame:
        return self.encode("hello_ok", {"server_name": server_name})

    def encode_shutdown_ok(self) -> ProtocolFrame:
        return self.encode("shutdown_ok", {})
