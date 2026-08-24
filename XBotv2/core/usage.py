"""Provider-neutral per-request usage contract."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

USAGE_FIELDS = (
    "input_tokens",
    "output_tokens",
    "total_tokens",
    "requests",
    "context_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
    "prompt_cache_write_tokens",
)


@dataclass(frozen=True, slots=True)
class UsageDelta:
    input_tokens: int
    output_tokens: int
    total_tokens: int
    requests: int
    context_tokens: int
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    prompt_cache_write_tokens: int = 0

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> "UsageDelta":
        unknown = set(value) - set(USAGE_FIELDS)
        if unknown:
            raise ValueError("Unknown usage fields: " + ", ".join(sorted(unknown)))
        input_tokens = _tokens(value, "input_tokens")
        output_tokens = _tokens(value, "output_tokens")
        cache_read = _tokens(value, "cache_read_input_tokens")
        cache_creation = _tokens(value, "cache_creation_input_tokens")
        cache_write = _tokens(value, "prompt_cache_write_tokens")
        total = _optional_tokens(
            value,
            "total_tokens",
            input_tokens + output_tokens + cache_read + cache_creation + cache_write,
        )
        context = _optional_tokens(
            value,
            "context_tokens",
            input_tokens + cache_read + cache_creation + cache_write,
        )
        return cls(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total,
            requests=_optional_tokens(value, "requests", 1 if value else 0),
            context_tokens=context,
            cache_read_input_tokens=cache_read,
            cache_creation_input_tokens=cache_creation,
            prompt_cache_write_tokens=cache_write,
        )

    def is_empty(self) -> bool:
        return not any(getattr(self, field) for field in USAGE_FIELDS)

    def to_event_dict(self) -> dict[str, int]:
        result = {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "requests": self.requests,
            "context_tokens": self.context_tokens,
        }
        for field in USAGE_FIELDS[5:]:
            value = getattr(self, field)
            if value:
                result[field] = value
        return result


def normalize_usage(value: Mapping[str, object]) -> dict[str, int]:
    return UsageDelta.from_mapping(value).to_event_dict() if value else {}


def _tokens(value: Mapping[str, object], field: str) -> int:
    raw = value.get(field, 0)
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
        raise ValueError(f"{field} must be a non-negative integer")
    return raw


def _optional_tokens(
    value: Mapping[str, object],
    field: str,
    default: int,
) -> int:
    return default if field not in value else _tokens(value, field)


__all__ = ["USAGE_FIELDS", "UsageDelta", "normalize_usage"]
