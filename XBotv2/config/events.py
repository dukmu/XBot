"""Typed configuration notifications owned by Settings."""

from __future__ import annotations

from dataclasses import dataclass

from XBotv2.config.models import RuntimeConfig


POLICY_CHANGED = "config/policy-changed"


@dataclass(frozen=True, slots=True)
class PolicyChanged:
    policy: dict[str, object]
    config: RuntimeConfig


__all__ = ["POLICY_CHANGED", "PolicyChanged"]
