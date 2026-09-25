"""Timing projections derived from canonical conversation messages."""

from __future__ import annotations

from collections.abc import Iterable

from pydantic import BaseModel, ConfigDict, Field, field_serializer

from XBotv2.core.messages import AssistantMessage, ConversationMessage, HumanInputMessage, ToolMessage


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
        return SessionStats(
            turns=self.turns + other.turns,
            steps=self.steps + other.steps,
            llm_ms=self.llm_ms + other.llm_ms,
            tool_ms=self.tool_ms + other.tool_ms,
            ttft_ms=self.ttft_ms + other.ttft_ms,
            ttft_steps=self.ttft_steps + other.ttft_steps,
            decode_ms=self.decode_ms + other.decode_ms,
            decode_tokens=self.decode_tokens + other.decode_tokens,
        )

    @field_serializer("llm_ms", "tool_ms", "ttft_ms", "decode_ms")
    def _round_milliseconds(self, value: float) -> float:
        return round(value, 3)


def conversation_stats(messages: Iterable[ConversationMessage]) -> SessionStats:
    stats = SessionStats()
    for message in messages:
        if isinstance(message, HumanInputMessage):
            stats = stats.add(SessionStats(turns=1))
        elif isinstance(message, AssistantMessage):
            timing = message.exchange.timing
            counters = message.exchange.usage.counters
            stats = stats.add(SessionStats(
                steps=1,
                llm_ms=timing.total_ms,
                ttft_ms=timing.first_delta_ms or 0.0,
                ttft_steps=1 if timing.first_delta_ms is not None else 0,
                decode_ms=timing.decode_ms or 0.0,
                decode_tokens=counters.output if timing.decode_ms is not None else 0,
            ))
        elif isinstance(message, ToolMessage):
            stats = stats.add(SessionStats(tool_ms=message.timing.duration_ms))
    return stats


__all__ = ["SessionStats", "conversation_stats"]
