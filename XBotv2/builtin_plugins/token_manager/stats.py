"""Token statistics collector — per-turn and cumulative tracking."""

from __future__ import annotations

import json
import time as _time
from dataclasses import dataclass, field
from pathlib import Path
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
        self.cumulative_prompt = 0
        self.cumulative_completion = 0
        self.cumulative_cache_hit = 0
        self.cumulative_cache_miss = 0

    def start_turn(self, turn: int, *, provider: str = "", model: str = "", context_msg_count: int = 0, estimated_prompt: int = 0) -> None:
        self._current = TurnStats(
            turn=turn, started_at=_time.monotonic(),
            provider=provider, model=model,
            context_messages=context_msg_count,
            estimated_prompt=estimated_prompt,
        )

    def record_usage(self, input_tokens: int, output_tokens: int, *, cache_hit: int = 0, cache_miss: int = 0, cache_write: int = 0) -> None:
        if self._current is None:
            return
        self._current.prompt_tokens += input_tokens
        self._current.completion_tokens += output_tokens
        self._current.cache_hit_tokens += cache_hit
        self._current.cache_miss_tokens += cache_miss
        self._current.cache_write_tokens += cache_write
        self.cumulative_prompt += input_tokens
        self.cumulative_completion += output_tokens
        self.cumulative_cache_hit += cache_hit
        self.cumulative_cache_miss += cache_miss

    def record_tool_call(self) -> None:
        if self._current:
            self._current.tool_calls += 1

    def finish_turn(self) -> TurnStats | None:
        if self._current is None:
            return None
        self._current.finished_at = _time.monotonic()
        self.turns.append(self._current)
        result = self._current
        self._current = None
        return result

    def summary(self) -> dict[str, Any]:
        return {
            "turns": len(self.turns),
            "cumulative_prompt_tokens": self.cumulative_prompt,
            "cumulative_completion_tokens": self.cumulative_completion,
            "cumulative_cache_hit_tokens": self.cumulative_cache_hit,
            "cumulative_cache_miss_tokens": self.cumulative_cache_miss,
            "last_turn": _turn_to_dict(self._current) if self._current else None,
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
