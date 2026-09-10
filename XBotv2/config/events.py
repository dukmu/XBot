"""Typed configuration notifications owned by Settings."""

from __future__ import annotations

from dataclasses import dataclass
from pydantic import JsonValue

POLICY_CHANGED = "config/policy-changed"


@dataclass(frozen=True, slots=True)
class PolicyChanged:
    policy: dict[str, JsonValue]
    effective_permissions: dict[str, JsonValue]
    effective_sandbox: dict[str, JsonValue]


__all__ = ["POLICY_CHANGED", "PolicyChanged"]
