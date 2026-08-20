"""Wire models owned by the permission-request capability."""

from typing import Any, Literal

from pydantic import Field, model_validator

from XBotv2.protocol import WireModel


class PermissionResponseRequest(WireModel):
    request_id: str = Field(min_length=1)
    decision: Literal["allow", "deny"]
    scope: Literal["once", "session"] = "once"


class RequestedPermissionData(WireModel):
    tool: str = Field(min_length=1)
    params: dict[str, str] = Field(default_factory=dict)


class PermissionRequestData(WireModel):
    request_id: str = Field(min_length=1)
    source: str = Field(min_length=1)
    tool_call: dict[str, Any] | None = None
    permission: RequestedPermissionData | None = None
    decision: Literal["ask"] = "ask"
    reason: str
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
    tool_call: dict[str, Any]
    decision: Literal["deny"] = "deny"
    reason: str
    resume_supported: bool = False


__all__ = [
    "PermissionDeniedData",
    "PermissionRequestData",
    "PermissionResponseRequest",
    "RequestedPermissionData",
]
