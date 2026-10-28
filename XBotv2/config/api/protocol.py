"""Transport models owned by this plugin."""
from __future__ import annotations
from typing import Literal
from pydantic import Field, JsonValue, StrictBool, field_validator, model_validator
from XBotv2.protocol.api.models import WireModel
from XBotv2.permissions.api.models import PermissionPolicy
from XBotv2.sandbox.api.models import SandboxConfig

PermissionDecision = Literal["allow", "deny", "ask"]


SandboxAccess = Literal["allow", "deny", "readonly", "readwrite"]


SandboxKey = Literal[
    "enabled",
    "network",
    "external_read",
    "external_write",
    "workspace_read",
    "workspace_write",
]


SandboxValue = StrictBool | SandboxAccess


class SessionPolicyPatch(WireModel):
    permissions: dict[str, PermissionDecision] = Field(default_factory=dict)
    remove_permissions: list[str] = Field(default_factory=list)
    sandbox: dict[SandboxKey, SandboxValue] = Field(default_factory=dict)
    remove_sandbox: list[SandboxKey] = Field(default_factory=list)

    @field_validator("permissions")
    @classmethod
    def _validate_permission_names(
        cls, value: dict[str, PermissionDecision]
    ) -> dict[str, PermissionDecision]:
        if any(not name.strip() for name in value):
            raise ValueError("permission tool names must be non-empty")
        return {name.strip(): decision for name, decision in value.items()}

    @field_validator("remove_permissions")
    @classmethod
    def _validate_removed_permission_names(cls, value: list[str]) -> list[str]:
        if any(not name.strip() for name in value):
            raise ValueError("permission tool names must be non-empty")
        return [name.strip() for name in value]

    @model_validator(mode="after")
    def _validate_policy_patch(self) -> "SessionPolicyPatch":
        permission_overlap = set(self.permissions).intersection(
            self.remove_permissions
        )
        sandbox_overlap = set(self.sandbox).intersection(self.remove_sandbox)
        if permission_overlap or sandbox_overlap:
            raise ValueError("policy keys cannot be set and removed together")
        for key, value in self.sandbox.items():
            if key in {"enabled", "network"} and not isinstance(value, bool):
                raise ValueError(f"sandbox.{key} must be a boolean")
            if key not in {"enabled", "network"} and isinstance(value, bool):
                raise ValueError(f"sandbox.{key} must be an access mode")
        return self


class SessionPolicyResponse(WireModel):
    session_id: str = Field(min_length=1)
    permissions: PermissionPolicy = Field(default_factory=PermissionPolicy)
    effective_permissions: PermissionPolicy = Field(default_factory=PermissionPolicy)
    sandbox: SandboxConfig = Field(default_factory=SandboxConfig)
    effective_sandbox: SandboxConfig = Field(default_factory=SandboxConfig)


class PluginConfigPatchRequest(WireModel):
    revision: str = Field(min_length=1)
    config: dict[str, JsonValue] = Field(default_factory=dict)
