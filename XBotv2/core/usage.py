"""Provider-neutral per-request usage contract."""

from __future__ import annotations

from collections.abc import Mapping
from pydantic import BaseModel, ConfigDict, Field


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
        return not any(self.model_dump().values())

    @classmethod
    def from_snapshot(cls, value: Mapping[str, object]) -> "UsageData":
        expected = {"schema_version", *USAGE_FIELDS}
        if set(value) != expected:
            raise ValueError("Usage snapshot fields do not match schema version 1")
        if _tokens(value, "schema_version") != 1:
            raise ValueError(f"Unsupported usage schema version: {value['schema_version']}")
        return cls(**{field: _tokens(value, field) for field in USAGE_FIELDS})

    def add(self, delta: "UsageData") -> "UsageData":
        current = self.model_dump()
        increment = delta.model_dump()
        totals = {
            field: current[field] + increment[field]
            for field in USAGE_FIELDS
        }
        totals["context_tokens"] = delta.context_tokens
        return UsageData(**totals)

    def totals(self) -> dict[str, int]:
        return self.model_dump()

    def to_snapshot(self) -> dict[str, int]:
        return {"schema_version": 1, **self.totals()}

    def to_event_dict(self) -> dict[str, int]:
        result = self.totals()
        for field in USAGE_FIELDS[5:]:
            if not result[field]:
                result.pop(field)
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


# Keep all derived usage projections tied to the Pydantic field declaration.
# Consumers must not maintain a second copy when a provider adds a counter.
USAGE_FIELDS = tuple(UsageData.model_fields)
USAGE_COUNTER_FIELDS = tuple(
    field for field in USAGE_FIELDS if field != "context_tokens"
)
INPUT_USAGE_FIELDS = (
    "input_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
    "prompt_cache_write_tokens",
)


__all__ = [
    "INPUT_USAGE_FIELDS",
    "USAGE_COUNTER_FIELDS",
    "USAGE_FIELDS",
    "UsageData",
    "normalize_usage",
]
