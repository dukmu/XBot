"""Approval contracts owned by the permissions plugin."""

from typing import Literal

from pydantic import Field, model_validator

from XBotv2.protocol import WireModel
from XBotv2.core.tools import ToolCall


class ApprovalDecision(WireModel):
    decision: Literal["allow", "deny"]
    scope: Literal["once", "session"] = "once"


class PermissionResponseRequest(ApprovalDecision):
    request_id: str = Field(min_length=1)


class RequestedPermissionData(WireModel):
    tool: str = Field(min_length=1)
    params: dict[str, str] = Field(default_factory=dict)


class PermissionRequestData(WireModel):
    request_id: str = Field(min_length=1)
    source: str = Field(min_length=1)
    tool_call: ToolCall | None = None
    permission: RequestedPermissionData | None = None
    decision: Literal["ask"] = "ask"
    reason: str
    # True when an unanswered request is replayed by a session snapshot, so a
    # client that reconnects can rebuild the dialog instead of losing it.
    resume_supported: bool = False

    @model_validator(mode="after")
    def _require_subject(self) -> "PermissionRequestData":
        if (self.tool_call is None) == (self.permission is None):
            raise ValueError(
                "permission request requires exactly one tool_call or permission"
            )
        return self


class PermissionDeniedData(WireModel):
    request_id: str = Field(min_length=1)
    source: str = Field(min_length=1)
    tool_call: ToolCall
    decision: Literal["deny"] = "deny"
    reason: str
    resume_supported: bool = False


__all__ = [
    "ApprovalDecision",
    "PermissionDeniedData",
    "PermissionRequestData",
    "PermissionResponseRequest",
    "RequestedPermissionData",
]
