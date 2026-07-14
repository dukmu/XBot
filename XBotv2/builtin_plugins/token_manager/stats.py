"""Token statistics collector — per-turn and cumulative tracking."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any


@dataclass
class TurnStats:
    turn: int
    started_at: float = 0.0
    finished_at: float = 0.0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cache_hit_tokens: int = 0
    cache_miss_tokens: int = 0
    cache_write_tokens: int = 0
    provider: str = ""
    model: str = ""
    tool_calls: int = 0
    context_messages: int = 0
    estimated_prompt: int = 0


class TokenStatsCollector:
    def __init__(self) -> None:
        self.turns: list[TurnStats] = []
        self._current: TurnStats | None = None

    def reset(self) -> None:
        self.turns.clear()
        self._current = None

    def start_turn(
        self,
        turn: int,
        *,
        provider: str = "",
        model: str = "",
        context_msg_count: int = 0,
        estimated_prompt: int = 0,
    ) -> None:
        self._current = TurnStats(
            turn=turn,
            started_at=time.monotonic(),
            provider=provider,
            model=model,
            context_messages=context_msg_count,
            estimated_prompt=estimated_prompt,
        )

    def record_usage(
        self,
        input_tokens: int,
        output_tokens: int,
        *,
        cache_hit: int = 0,
        cache_miss: int = 0,
        cache_write: int = 0,
    ) -> None:
        if self._current is None:
            return
        self._current.prompt_tokens += input_tokens
        self._current.completion_tokens += output_tokens
        self._current.cache_hit_tokens += cache_hit
        self._current.cache_miss_tokens += cache_miss
        self._current.cache_write_tokens += cache_write

    def record_tool_call(self) -> None:
        if self._current:
            self._current.tool_calls += 1

    def update_context(self, *, estimated_prompt: int, message_count: int) -> None:
        if self._current is None:
            return
        self._current.estimated_prompt = estimated_prompt
        self._current.context_messages = message_count

    def finish_turn(self) -> TurnStats | None:
        if self._current is None:
            return None
        self._current.finished_at = time.monotonic()
        self.turns.append(self._current)
        result = self._current
        self._current = None
        return result

    def summary(self) -> dict[str, Any]:
        latest = self._current or (self.turns[-1] if self.turns else None)
        stats = list(self.turns)
        if self._current is not None:
            stats.append(self._current)
        return {
            "turns": len(self.turns),
            "cumulative_prompt_tokens": sum(t.prompt_tokens for t in stats),
            "cumulative_completion_tokens": sum(t.completion_tokens for t in stats),
            "cumulative_cache_hit_tokens": sum(t.cache_hit_tokens for t in stats),
            "cumulative_cache_miss_tokens": sum(t.cache_miss_tokens for t in stats),
            "last_turn": _turn_to_dict(latest) if latest else None,
        }


def _turn_to_dict(t: TurnStats) -> dict[str, Any]:
    return {
        "turn": t.turn,
        "started_at": t.started_at,
        "finished_at": t.finished_at,
        "prompt_tokens": t.prompt_tokens,
        "completion_tokens": t.completion_tokens,
        "cache_hit_tokens": t.cache_hit_tokens,
        "cache_miss_tokens": t.cache_miss_tokens,
        "cache_write_tokens": t.cache_write_tokens,
        "provider": t.provider,
        "model": t.model,
        "tool_calls": t.tool_calls,
        "context_messages": t.context_messages,
        "estimated_prompt": t.estimated_prompt,
    }
