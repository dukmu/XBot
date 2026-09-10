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
)

from XBotv2.core.operations import EmptyRequest, Operation
from XBotv2.loader.contracts import PluginTree


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class UserContext(StrictModel):
    user_id: str = "default-user"
    user_name: str = "User"
    platform: str = "terminal"
    session_type: str = "interactive"


class ConfigPluginConfig(StrictModel):
    """Configuration owned by the configuration plugin itself."""

    user: UserContext = Field(default_factory=UserContext)
    instructions: str = ""


class PluginConfig(StrictModel):
    enabled: bool = True
    config: dict[str, JsonValue] = Field(default_factory=dict)


class RuntimeConfig(StrictModel):
    """Agent runtime projection built from a generic plugin tree.

    This model is not a plugin declaration.  Plugin-owned settings such as
    permissions, sandbox, tools, and provider catalogs remain in their owner
    plugins and are never copied into this contract.
    """

    provider: str = "default"
    tools: list[str] | None = None
    plugins: dict[str, PluginConfig] = Field(default_factory=dict)
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
    permissions: dict[str, str] | None = None
    remove_permissions: tuple[str, ...] = ()
    sandbox: dict[str, JsonValue] | None = None
    remove_sandbox: tuple[str, ...] = ()


class SettingsPort(Protocol):
    """Session-bound configuration service mounted as ``ctx.settings``."""

    def user_context(self) -> UserContext: ...
    def load_plugin_tree(
        self,
        workspace: Path,
        session_id: str,
    ) -> PluginTree: ...
    def memory(self) -> str: ...
    def policy(self) -> PolicySnapshot: ...
    async def update_policy(self, patch: PatchPolicy) -> PolicySnapshot: ...
    def plugin_config_catalog(
        self,
        workspace: Path,
        scope: PluginConfigScope,
        session_id: str,
    ) -> PluginConfigCatalog: ...
    def update_plugin_config(
        self,
        workspace: Path,
        plugin_id: str,
        patch: PatchPluginConfig,
        session_id: str,
    ) -> PluginConfigCatalog: ...


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
    "UserContext",
]
