"""Durable timing metadata and conversation-level statistics."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_serializer

from XBotv2.core.messages import Message

TIMING_METADATA_KEY = "xbot_timing"
SESSION_STATS_METADATA_KEY = "xbot_session_stats"


class SessionStats(BaseModel):
    turns: int = Field(default=0, ge=0)
    steps: int = Field(default=0, ge=0)
    llm_ms: float = Field(default=0.0, ge=0)
    tool_ms: float = Field(default=0.0, ge=0)
    ttft_ms: float = Field(default=0.0, ge=0)
    ttft_steps: int = Field(default=0, ge=0)
    decode_ms: float = Field(default=0.0, ge=0)
    decode_tokens: int = Field(default=0, ge=0)
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    def add(self, other: "SessionStats") -> "SessionStats":
        current = self.model_dump()
        increment = other.model_dump()
        return SessionStats(**{
            name: current[name] + increment[name]
            for name in type(self).model_fields
        })

    @field_serializer("llm_ms", "tool_ms", "ttft_ms", "decode_ms")
    def _round_milliseconds(self, value: float) -> float:
        return round(value, 3)


def conversation_stats(messages: Iterable[Message]) -> SessionStats:
    """Fold visible history, including statistics retained by compaction."""
    stats = SessionStats()
    for message in messages:
        retained = _stats(message.response_metadata.get(SESSION_STATS_METADATA_KEY))
        if retained is not None:
            stats = stats.add(retained)
        if message.role == "user":
            if "runtime_input" not in message.additional_kwargs:
                stats = stats.add(SessionStats(turns=1))
            continue
        timing = _timing(message.response_metadata.get(TIMING_METADATA_KEY))
        if timing is None:
            continue
        if message.role == "assistant":
            ttft = timing.get("ttft_ms")
            decode = timing.get("decode_ms")
            output_tokens = _nonnegative_int(message.usage_metadata.get("output_tokens"))
            stats = stats.add(SessionStats(
                steps=1,
                llm_ms=timing.get("llm_ms", 0.0),
                ttft_ms=ttft or 0.0,
                ttft_steps=1 if ttft is not None else 0,
                decode_ms=decode or 0.0,
                decode_tokens=output_tokens if decode is not None else 0,
            ))
        elif message.role == "tool":
            stats = stats.add(SessionStats(tool_ms=timing.get("duration_ms", 0.0)))
    return stats


def _timing(value: Any) -> dict[str, float] | None:
    if not isinstance(value, Mapping):
        return None
    result: dict[str, float] = {}
    for key, number in value.items():
        if (
            not isinstance(key, str)
            or isinstance(number, bool)
            or not isinstance(number, (int, float))
            or number < 0
        ):
            raise ValueError("Persisted timing metadata is malformed")
        result[key] = float(number)
    return result


def _stats(value: Any) -> SessionStats | None:
    if not isinstance(value, Mapping):
        return None
    return SessionStats.model_validate(value)


def _nonnegative_int(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return 0
    return value


__all__ = [
    "SESSION_STATS_METADATA_KEY",
    "TIMING_METADATA_KEY",
    "SessionStats",
    "conversation_stats",
]
