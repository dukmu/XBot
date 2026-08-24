"""Strict usage delta and cumulative snapshot models."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from XBotv2.core.usage import USAGE_FIELDS, UsageDelta

USAGE_SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class UsageSnapshot:
    schema_version: int = USAGE_SCHEMA_VERSION
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    requests: int = 0
    context_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    prompt_cache_write_tokens: int = 0

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> "UsageSnapshot":
        expected = {"schema_version", *USAGE_FIELDS}
        if set(value) != expected:
            raise ValueError("UsageSnapshot fields do not match schema version 1")
        version = _tokens(value, "schema_version")
        if version != USAGE_SCHEMA_VERSION:
            raise ValueError(f"Unsupported UsageSnapshot schema version: {version}")
        return cls(
            schema_version=version,
            **{field: _tokens(value, field) for field in USAGE_FIELDS},
        )

    def add(self, delta: UsageDelta) -> "UsageSnapshot":
        return UsageSnapshot(
            input_tokens=self.input_tokens + delta.input_tokens,
            output_tokens=self.output_tokens + delta.output_tokens,
            total_tokens=self.total_tokens + delta.total_tokens,
            requests=self.requests + delta.requests,
            context_tokens=delta.context_tokens,
            cache_read_input_tokens=(
                self.cache_read_input_tokens + delta.cache_read_input_tokens
            ),
            cache_creation_input_tokens=(
                self.cache_creation_input_tokens
                + delta.cache_creation_input_tokens
            ),
            prompt_cache_write_tokens=(
                self.prompt_cache_write_tokens + delta.prompt_cache_write_tokens
            ),
        )

    def to_dict(self) -> dict[str, int]:
        return {
            "schema_version": self.schema_version,
            **{field: getattr(self, field) for field in USAGE_FIELDS},
        }

    def totals(self) -> dict[str, int]:
        return {field: getattr(self, field) for field in USAGE_FIELDS}


def _tokens(value: Mapping[str, object], field: str) -> int:
    raw = value.get(field, 0)
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
        raise ValueError(f"{field} must be a non-negative integer")
    return raw


__all__ = ["USAGE_FIELDS", "UsageDelta", "UsageSnapshot"]
