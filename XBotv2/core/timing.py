"""Durable timing metadata and conversation-level statistics."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, fields
from typing import Any

from XBotv2.core.messages import Message

TIMING_METADATA_KEY = "xbot_timing"
SESSION_STATS_METADATA_KEY = "xbot_session_stats"


@dataclass(frozen=True, slots=True)
class SessionStats:
    turns: int = 0
    steps: int = 0
    llm_ms: float = 0.0
    tool_ms: float = 0.0
    ttft_ms: float = 0.0
    ttft_steps: int = 0
    decode_ms: float = 0.0
    decode_tokens: int = 0

    def add(self, other: "SessionStats") -> "SessionStats":
        return SessionStats(**{
            field.name: getattr(self, field.name) + getattr(other, field.name)
            for field in fields(self)
        })

    def to_dict(self) -> dict[str, int | float]:
        return {
            "turns": self.turns,
            "steps": self.steps,
            "llm_ms": round(self.llm_ms, 3),
            "tool_ms": round(self.tool_ms, 3),
            "ttft_ms": round(self.ttft_ms, 3),
            "ttft_steps": self.ttft_steps,
            "decode_ms": round(self.decode_ms, 3),
            "decode_tokens": self.decode_tokens,
        }


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
    expected = {field.name for field in fields(SessionStats)}
    if set(value) != expected:
        raise ValueError("Persisted session statistics fields are malformed")
    return SessionStats(
        turns=_required_int(value["turns"]),
        steps=_required_int(value["steps"]),
        llm_ms=_required_float(value["llm_ms"]),
        tool_ms=_required_float(value["tool_ms"]),
        ttft_ms=_required_float(value["ttft_ms"]),
        ttft_steps=_required_int(value["ttft_steps"]),
        decode_ms=_required_float(value["decode_ms"]),
        decode_tokens=_required_int(value["decode_tokens"]),
    )


def _required_float(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        raise ValueError("Persisted session timing must be non-negative")
    return float(value)


def _required_int(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("Persisted session counts must be non-negative integers")
    return value


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
