"""Typed policy operations owned by Settings."""

from __future__ import annotations

from dataclasses import dataclass
from xcore.api.services import ServiceKey
from pathlib import Path
from typing import Literal, Protocol
from pydantic import BaseModel, ConfigDict, Field, JsonValue
from XBotv2.core.api.operations import EmptyRequest, Operation
import XBotv2.core.api.domain as domain
from XBotv2.loader.api.contracts import PluginTree
from XBotv2.permissions.api.models import PermissionPolicy
from XBotv2.agentloop.api.contracts import AllTools, ToolSelection


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ConfigPluginConfig(StrictModel):
    """Configuration owned by the configuration plugin itself."""

    user: domain.UserContext = Field(default_factory=domain.UserContext)
    instructions: str = ""


class PluginConfig(StrictModel):
    enabled: bool = True
    config: dict[str, JsonValue] = Field(default_factory=dict)


class RuntimeConfig(StrictModel):
    """Resolved scalar inputs consumed while constructing a runtime selection."""

    enabled_tools: ToolSelection = Field(default_factory=AllTools)
    instructions: str = ""


@dataclass(frozen=True, slots=True)
class PolicySnapshot:
    policy: dict[str, JsonValue]
    effective_permissions: dict[str, JsonValue]
    effective_sandbox: dict[str, JsonValue]


PluginConfigScope = Literal["global", "workspace", "session"]


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
    applies_to: Literal["new_sessions", "current_session"] = "new_sessions"
    plugins: list[PluginConfigDescriptor] = Field(default_factory=list)


class PluginConfigConflict(RuntimeError):
    """The caller attempted to write an obsolete overlay revision."""


class PluginConfigUnavailable(ValueError):
    """The selected plugin has no generic, producer-declared schema."""


class PatchPluginConfig(StrictModel):
    scope: PluginConfigScope
    revision: str = Field(min_length=1)
    config: dict[str, JsonValue] = Field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class PatchPolicy:
    replace_permissions: dict[str, JsonValue] | None = None
    replace_sandbox: dict[str, JsonValue] | None = None
    permissions: dict[str, str] | None = None
    remove_permissions: tuple[str, ...] = ()
    sandbox: dict[str, JsonValue] | None = None
    remove_sandbox: tuple[str, ...] = ()
    revision_key: Literal["permissions", "sandbox"] | None = None
    expected_revision: str | None = None


class SettingsPort(Protocol):
    """Session-bound configuration service mounted as ``ctx.settings``."""

    def user_context(self) -> domain.UserContext: ...
    def load_plugin_tree(
        self,
        workspace: Path,
        session_id: str,
        thread_id: str | None = None,
    ) -> PluginTree: ...
    def memory(self) -> str: ...
    def permission_policies(self) -> tuple[PermissionPolicy, ...]: ...
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
    "GET_POLICY",
    "ConfigPluginConfig",
    "PluginConfig",
    "UPDATE_POLICY",
    "PatchPolicy",
    "PatchPluginConfig",
    "PolicySnapshot",
    "PluginConfigCatalog",
    "PluginConfigConflict",
    "PluginConfigDescriptor",
    "PluginConfigScope",
    "PluginConfigUnavailable",
    "RuntimeConfig",
    "SettingsPort",
]

SETTINGS = ServiceKey[SettingsPort]("settings")
