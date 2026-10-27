"""Typed notifications owned by the permissions plugin."""

from __future__ import annotations

from dataclasses import dataclass
from xcore.api.events import EventKey
from XBotv2.permissions.api.models import Approval, PermissionRequest, PermissionRule




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

PERMISSION_DECIDED = EventKey[PermissionDecisionRecorded]("permissions/decided")
PERMISSION_REQUESTED = EventKey[PermissionRequested]("permission/request")
