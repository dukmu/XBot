"""Typed notifications owned by the permissions plugin."""

from __future__ import annotations

from dataclasses import dataclass
from XBotv2.permissions.contracts import Approval, PermissionRequest, PermissionRule


PERMISSION_DECIDED = "permissions/decided"
PERMISSION_REQUESTED = "permission/request"


@dataclass(frozen=True, slots=True)
class PermissionDecisionRecorded:
    request: PermissionRequest
    approval: Approval
    rule: PermissionRule


@dataclass(frozen=True, slots=True)
class PermissionRequested:
    request: PermissionRequest


__all__ = [
    "PERMISSION_DECIDED",
    "PERMISSION_REQUESTED",
    "PermissionDecisionRecorded",
    "PermissionRequested",
]
