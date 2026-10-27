"""Typed configuration notifications owned by Settings."""

from __future__ import annotations

from dataclasses import dataclass
from xcore.api.events import EventKey
from pydantic import JsonValue
from XBotv2.permissions.api.models import PermissionPolicy



@dataclass(frozen=True, slots=True)
class PolicyChanged:
    policy: dict[str, JsonValue]
    permission_policies: tuple[PermissionPolicy, ...]
    effective_sandbox: dict[str, JsonValue]


POLICY_CHANGED = EventKey[PolicyChanged]("config/policy-changed")

__all__ = ["POLICY_CHANGED", "PolicyChanged"]
