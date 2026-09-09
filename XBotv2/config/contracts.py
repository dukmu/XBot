"""Typed policy operations owned by Settings."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    field_validator,
    model_validator,
)

from XBotv2.core.operations import EmptyRequest, Operation


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class UserContext(StrictModel):
    user_id: str = "default-user"
    user_name: str = "User"
    platform: str = "terminal"
    session_type: str = "interactive"


class HookConfig(StrictModel):
    stage: str
    target: str
    base_dir: Path | None = Field(default=None, exclude=True)

    @field_validator("target")
    @classmethod
    def _validate_target(cls, value: str) -> str:
        source, separator, handler = value.partition(":")
        if not separator or not source or not handler:
            raise ValueError("target must use source:handler syntax")
        return value


class WorkspaceToolConfig(StrictModel):
    """One explicit Tool export from the workspace's ``.xbot/tools`` directory."""

    target: str
    base_dir: Path | None = Field(default=None, exclude=True)

    @field_validator("target")
    @classmethod
    def _validate_target(cls, value: str) -> str:
        source, separator, export = value.partition(":")
        if not separator or not source or not export:
            raise ValueError("target must use tools/module.py:export syntax")
        return value


class PluginConfig(StrictModel):
    enabled: bool = True
    config: dict[str, JsonValue] = Field(default_factory=dict)


class PermissionRuleConfig(StrictModel):
    tool: str = ".*"
    params: dict[str, str] = Field(default_factory=dict)
    paths: str | None = None


class PermissionConfig(StrictModel):
    deny: list[PermissionRuleConfig] = Field(default_factory=list)
    allow: list[PermissionRuleConfig] = Field(default_factory=list)
    ask: list[PermissionRuleConfig] = Field(default_factory=list)


class SandboxResourceConfig(StrictModel):
    path: str
    access: Literal["allow", "readwrite", "readonly", "deny"] = "readonly"


class SandboxConfig(StrictModel):
    enabled: bool = True
    network: bool = True
    external_read: Literal["allow", "readwrite", "readonly", "deny"] = "readonly"
    external_write: Literal["allow", "readwrite", "readonly", "deny"] = "deny"
    workspace_read: Literal["allow", "readwrite", "readonly", "deny"] = "allow"
    workspace_write: Literal["allow", "readwrite", "readonly", "deny"] = "allow"
    resources: list[SandboxResourceConfig] = Field(default_factory=list)


class ToolResultConfig(StrictModel):
    cache_threshold_chars: int = Field(default=12_000, ge=1)
    preview_chars: int = Field(default=8_000, ge=0)
    tail_chars: int = Field(default=2_000, ge=0)

    @model_validator(mode="after")
    def _validate_preview(self) -> "ToolResultConfig":
        if self.preview_chars > self.cache_threshold_chars:
            raise ValueError("preview_chars cannot exceed cache_threshold_chars")
        if self.tail_chars > self.preview_chars:
            raise ValueError("tail_chars cannot exceed preview_chars")
        return self


class ConfigOverlay(StrictModel):
    """One partial global, session, or workspace configuration layer."""

    provider: str | None = None
    max_concurrent_subagents: int | None = Field(default=None, ge=1)
    tool_results: ToolResultConfig | None = None
    tools: list[str] | None = None
    workspace_tools: list[WorkspaceToolConfig] | None = None
    hooks: list[HookConfig] | None = None
    plugins: dict[str, PluginConfig] | None = None
    plugin_paths: list[str] | None = None
    permissions: PermissionConfig | None = None
    sandbox: SandboxConfig | None = None
    instructions: str | None = None


class RuntimeConfig(StrictModel):
    """Complete runtime configuration resolved from all configuration layers."""

    provider: str = "default"
    max_concurrent_subagents: int = Field(default=4, ge=1)
    tool_results: ToolResultConfig = Field(default_factory=ToolResultConfig)
    tools: list[str] | None = None
    workspace_tools: list[WorkspaceToolConfig] = Field(default_factory=list)
    hooks: list[HookConfig] = Field(default_factory=list)
    plugins: dict[str, PluginConfig] = Field(default_factory=dict)
    plugin_paths: list[str] = Field(default_factory=list)
    permissions: PermissionConfig = Field(default_factory=lambda: PermissionConfig(
        ask=[PermissionRuleConfig(tool=".*")]
    ))
    sandbox: SandboxConfig = Field(default_factory=SandboxConfig)
    instructions: str = ""
    memory: str = ""
    agent_name: str = Field(default="XBotv2", exclude=True)
    agent_role: str = Field(default="", exclude=True)
    agent_instructions: str = ""
    max_context_tokens: int = Field(default=32_000, ge=1, exclude=True)
    max_output_tokens: int | None = Field(default=None, ge=1, exclude=True)

    @property
    def plugin_configs(self) -> dict[str, dict[str, JsonValue]]:
        return {
            name: entry.config
            for name, entry in self.plugins.items()
            if entry.enabled
        }


def config_dict(
    value: BaseModel | dict[str, JsonValue] | None,
) -> dict[str, JsonValue]:
    if value is None:
        return {}
    if isinstance(value, BaseModel):
        return value.model_dump(exclude_none=True)
    return dict(value)


@dataclass(frozen=True, slots=True)
class PolicySnapshot:
    policy: dict[str, JsonValue]
    effective_permissions: dict[str, JsonValue]
    effective_sandbox: dict[str, JsonValue]


PluginConfigScope = Literal["global", "workspace"]


class PluginConfigDescriptor(StrictModel):
    """One plugin configuration declaration projected for a generic client."""

    plugin_id: str
    name: str
    editable: bool
    config_schema: dict[str, JsonValue] | None = None
    scope_config: dict[str, JsonValue] = Field(default_factory=dict)
    effective_config: dict[str, JsonValue] = Field(default_factory=dict)
    unavailable_reason: str = ""


class PluginConfigCatalog(StrictModel):
    scope: PluginConfigScope
    workspace_root: str
    revision: str
    applies_to: Literal["new_sessions"] = "new_sessions"
    plugins: list[PluginConfigDescriptor] = Field(default_factory=list)


class PatchPluginConfig(StrictModel):
    scope: PluginConfigScope
    revision: str = Field(min_length=1)
    config: dict[str, JsonValue] = Field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class PatchPolicy:
    permissions: dict[str, str] | None = None
    remove_permissions: tuple[str, ...] = ()
    sandbox: dict[str, JsonValue] | None = None
    remove_sandbox: tuple[str, ...] = ()


class SettingsPort(Protocol):
    """Session-bound configuration service mounted as ``ctx.settings``."""

    def user_context(self) -> UserContext: ...
    def load_runtime_config(
        self,
        workspace: Path,
        session_id: str,
    ) -> RuntimeConfig: ...
    def policy(self) -> PolicySnapshot: ...
    async def update_policy(self, patch: PatchPolicy) -> PolicySnapshot: ...


GET_POLICY = Operation("config/policy/get", EmptyRequest, PolicySnapshot)
UPDATE_POLICY = Operation(
    "config/policy/update",
    PatchPolicy,
    PolicySnapshot,
    exclusive=True,
)


__all__ = [
    "ConfigOverlay",
    "GET_POLICY",
    "HookConfig",
    "PermissionConfig",
    "PermissionRuleConfig",
    "PluginConfig",
    "UPDATE_POLICY",
    "PatchPolicy",
    "PatchPluginConfig",
    "PolicySnapshot",
    "PluginConfigCatalog",
    "PluginConfigDescriptor",
    "PluginConfigScope",
    "RuntimeConfig",
    "SandboxConfig",
    "SandboxResourceConfig",
    "SettingsPort",
    "ToolResultConfig",
    "UserContext",
    "WorkspaceToolConfig",
    "config_dict",
]
