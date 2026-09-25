"""Public service contracts for permission policy and approval."""

from __future__ import annotations

from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from XBotv2.core.tools import ToolCall


PermissionDecision = Literal["allow", "deny", "ask"]


class PermissionRule(BaseModel):
    tool_pattern: str
    param_patterns: dict[str, str] = Field(default_factory=dict)
    path_scope: str | None = None
    decision: PermissionDecision
    model_config = ConfigDict(extra="forbid", frozen=True)


class PermissionPolicy(BaseModel):
    rules: tuple[PermissionRule, ...] = ()
    default_decision: PermissionDecision = "ask"
    model_config = ConfigDict(extra="forbid", frozen=True)


class ToolPermission(BaseModel):
    kind: Literal["tool"] = "tool"
    tool_call: ToolCall
    model_config = ConfigDict(extra="forbid", frozen=True)


class NamedPermission(BaseModel):
    kind: Literal["named"] = "named"
    tool: str = Field(min_length=1)
    params: dict[str, JsonValue] = Field(default_factory=dict)
    model_config = ConfigDict(extra="forbid", frozen=True)


PermissionSubject = ToolPermission | NamedPermission


class PermissionRequest(BaseModel):
    kind: Literal["permission_request"] = "permission_request"
    interaction_id: str = Field(min_length=1)
    source: str = Field(min_length=1)
    subject: PermissionSubject
    reason: str
    resume_supported: bool = False
    model_config = ConfigDict(extra="forbid", frozen=True)


class Allowed(BaseModel):
    kind: Literal["allowed"] = "allowed"
    scope: Literal["once", "session"] = "once"
    model_config = ConfigDict(extra="forbid", frozen=True)


class Denied(BaseModel):
    kind: Literal["denied"] = "denied"
    reason: str
    model_config = ConfigDict(extra="forbid", frozen=True)


Approval = Allowed | Denied


class ApprovalPort(Protocol):
    async def request(self, request: PermissionRequest) -> Approval: ...


class PermissionsPort(Protocol):
    def check(
        self,
        tool_name: str,
        args: dict[str, JsonValue] | None = None,
    ) -> Literal["allow", "deny", "ask"]: ...
    def explicit_allow(
        self,
        tool_name: str,
        args: dict[str, JsonValue] | None = None,
        *,
        constrain_param: str | None = None,
    ) -> bool: ...
    def check_tool_call(self, tool_call: ToolCall) -> tuple[str, str]: ...
    def grant_once(self, tool_name: str, param_patterns: dict[str, str]) -> None: ...
    def consume_once(self, tool_name: str, args: dict[str, JsonValue]) -> None: ...


__all__ = [
    "Allowed",
    "ApprovalPort",
    "Approval",
    "Denied",
    "PermissionDecision",
    "PermissionPolicy",
    "PermissionRequest",
    "PermissionRule",
    "PermissionSubject",
    "PermissionsPort",
    "NamedPermission",
    "ToolPermission",
]
