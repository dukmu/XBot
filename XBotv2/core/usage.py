"""Provider-neutral per-request usage contract."""

from __future__ import annotations

from collections.abc import Mapping
from pydantic import BaseModel, ConfigDict, Field

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


class UsageData(BaseModel):
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    total_tokens: int = Field(default=0, ge=0)
    requests: int = Field(default=0, ge=0)
    context_tokens: int = Field(default=0, ge=0)
    cache_read_input_tokens: int = Field(default=0, ge=0)
    cache_creation_input_tokens: int = Field(default=0, ge=0)
    prompt_cache_write_tokens: int = Field(default=0, ge=0)
    model_config = ConfigDict(extra="forbid", frozen=True)

    @classmethod
    def from_provider(cls, value: Mapping[str, object]) -> "UsageData":
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

    @classmethod
    def from_snapshot(cls, value: Mapping[str, object]) -> "UsageData":
        expected = {"schema_version", *USAGE_FIELDS}
        if set(value) != expected:
            raise ValueError("Usage snapshot fields do not match schema version 1")
        if _tokens(value, "schema_version") != 1:
            raise ValueError(f"Unsupported usage schema version: {value['schema_version']}")
        return cls(**{field: _tokens(value, field) for field in USAGE_FIELDS})

    def add(self, delta: "UsageData") -> "UsageData":
        totals = {
            field: getattr(self, field) + getattr(delta, field)
            for field in USAGE_FIELDS
        }
        totals["context_tokens"] = delta.context_tokens
        return UsageData(**totals)

    def totals(self) -> dict[str, int]:
        return {field: getattr(self, field) for field in USAGE_FIELDS}

    def to_snapshot(self) -> dict[str, int]:
        return {"schema_version": 1, **self.totals()}

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
    return UsageData.from_provider(value).to_event_dict() if value else {}


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


__all__ = ["USAGE_FIELDS", "UsageData", "normalize_usage"]
