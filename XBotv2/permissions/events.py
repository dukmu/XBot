"""Typed notifications owned by the permissions plugin."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from XBotv2.core.tools import JsonObject


PERMISSION_DECIDED = "permissions/decided"


@dataclass(frozen=True, slots=True)
class PermissionDecided:
    decision: Literal["allow", "deny"]
    scope: str
    rule: JsonObject


__all__ = ["PERMISSION_DECIDED", "PermissionDecided"]
