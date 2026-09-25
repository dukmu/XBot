"""Typed configuration notifications owned by Settings."""

from __future__ import annotations

from dataclasses import dataclass
from pydantic import JsonValue
from XBotv2.permissions.contracts import PermissionPolicy

POLICY_CHANGED = "config/policy-changed"


@dataclass(frozen=True, slots=True)
class PolicyChanged:
    policy: dict[str, JsonValue]
    permission_policies: tuple[PermissionPolicy, ...]
    effective_sandbox: dict[str, JsonValue]


__all__ = ["POLICY_CHANGED", "PolicyChanged"]
