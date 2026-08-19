"""Configuration parsing for the compact plugin."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from xcore import S


CONFIG_SCHEMA = S.object({
    "automatic": S.boolean().optional(),
    "output_reservation": S.number().optional(),
    "trigger_ratio": S.number().optional(),
    "keep_recent_turns": S.number().optional(),
    "summary_max_chars": S.number().optional(),
})


@dataclass(frozen=True, slots=True)
class CompactConfig:
    automatic: bool = True
    output_reservation: int | None = None
    trigger_ratio: float = 0.8
    keep_recent_turns: int = 4
    summary_max_chars: int = 8_000


def parse_compact_config(config: Any = None) -> CompactConfig:
    raw = dict(config or {})
    reservation = raw.get("output_reservation")
    return CompactConfig(
        automatic=bool(raw.get("automatic", True)),
        output_reservation=(
            _integer(reservation, "output_reservation", minimum=0)
            if reservation is not None
            else None
        ),
        trigger_ratio=_ratio(raw.get("trigger_ratio", 0.8)),
        keep_recent_turns=_integer(
            raw.get("keep_recent_turns", 4),
            "keep_recent_turns",
            minimum=1,
        ),
        summary_max_chars=_integer(
            raw.get("summary_max_chars", 8_000),
            "summary_max_chars",
            minimum=1,
        ),
    )


def _integer(value: Any, name: str, *, minimum: int) -> int:
    if isinstance(value, bool):
        raise ValueError(f"compact.{name} must be an integer >= {minimum}")
    number = float(value)
    if not number.is_integer():
        raise ValueError(f"compact.{name} must be an integer >= {minimum}")
    result = int(number)
    if result < minimum:
        raise ValueError(f"compact.{name} must be >= {minimum}")
    return result


def _ratio(value: Any) -> float:
    if isinstance(value, bool):
        raise ValueError("compact.trigger_ratio must be in (0, 1]")
    result = float(value)
    if not 0.0 < result <= 1.0:
        raise ValueError("compact.trigger_ratio must be in (0, 1]")
    return result


__all__ = ["CONFIG_SCHEMA", "CompactConfig", "parse_compact_config"]
