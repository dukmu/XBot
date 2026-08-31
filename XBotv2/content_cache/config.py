"""Validated configuration for current-user content caching."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from xcore import S


CONFIG_SCHEMA = S.object({
    "cache_threshold_chars": S.number().optional(),
    "preview_chars": S.number().optional(),
    "tail_chars": S.number().optional(),
})


@dataclass(frozen=True, slots=True)
class ContentCacheConfig:
    cache_threshold_chars: int = 48_000
    preview_chars: int = 12_000
    tail_chars: int = 2_000


def parse_content_cache_config(config: Any = None) -> ContentCacheConfig:
    raw = dict(config or {})
    parsed = ContentCacheConfig(
        cache_threshold_chars=_positive_integer(
            raw.get("cache_threshold_chars", 48_000),
            "cache_threshold_chars",
        ),
        preview_chars=_non_negative_integer(
            raw.get("preview_chars", 12_000),
            "preview_chars",
        ),
        tail_chars=_non_negative_integer(
            raw.get("tail_chars", 2_000),
            "tail_chars",
        ),
    )
    if parsed.preview_chars > parsed.cache_threshold_chars:
        raise ValueError(
            "content_cache.preview_chars cannot exceed cache_threshold_chars"
        )
    if parsed.tail_chars > parsed.preview_chars:
        raise ValueError("content_cache.tail_chars cannot exceed preview_chars")
    return parsed


def _positive_integer(value: Any, name: str) -> int:
    result = _non_negative_integer(value, name)
    if result == 0:
        raise ValueError(f"content_cache.{name} must be positive")
    return result


def _non_negative_integer(value: Any, name: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"content_cache.{name} must be a non-negative integer")
    number = float(value)
    if not number.is_integer() or number < 0:
        raise ValueError(f"content_cache.{name} must be a non-negative integer")
    return int(number)


__all__ = [
    "CONFIG_SCHEMA",
    "ContentCacheConfig",
    "parse_content_cache_config",
]
