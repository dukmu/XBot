"""Typed configuration notifications owned by Settings."""

from __future__ import annotations

from dataclasses import dataclass
from pydantic import JsonValue

from XBotv2.config.contracts import RuntimeConfig


POLICY_CHANGED = "config/policy-changed"


@dataclass(frozen=True, slots=True)
class PolicyChanged:
    policy: dict[str, JsonValue]
    config: RuntimeConfig


__all__ = ["POLICY_CHANGED", "PolicyChanged"]
