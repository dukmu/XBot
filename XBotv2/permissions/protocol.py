"""Approval contracts owned by the permissions plugin."""

from typing import Literal

from pydantic import Field

from XBotv2.protocol import WireModel
from XBotv2.permissions.contracts import Approval


class PermissionResponseRequest(WireModel):
    request_id: str = Field(min_length=1)
    decision: Literal["allow", "deny"]
    scope: Literal["once", "session"] = "once"


class PermissionResponseRecorded(WireModel):
    kind: Literal["permission_response_recorded"] = "permission_response_recorded"
    interaction_id: str = Field(min_length=1)
    approval: Approval
    pending_ids: tuple[str, ...] = ()


__all__ = [
    "PermissionResponseRequest",
    "PermissionResponseRecorded",
]
