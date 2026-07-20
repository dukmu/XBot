"""Validated configuration values and overlays."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from xbotv2.api.hooks import HookStage


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class UserContext(StrictModel):
    user_id: str = "default-user"
    user_name: str = "User"
    platform: str = "terminal"
    session_type: str = "interactive"


class ProviderConfig(StrictModel):
    provider: str = "openai"
    model: str = "gpt-4"
    base_url: str | None = None
    api_key: str | None = None
    temperature: float = 0.7
    max_context_tokens: int = Field(default=32_000, ge=1)
    max_output_tokens: int | None = Field(default=None, ge=1)
    reasoning_effort: str | None = None
    thinking_enabled: bool = False
    mock_responses: list[dict[str, Any]] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_anthropic_output_limit(self) -> "ProviderConfig":
        if (
            self.provider in {"anthropic", "lmstudio"}
            and self.max_output_tokens is None
        ):
            raise ValueError("Anthropic providers require max_output_tokens")
        return self

    @property
    def model_mode(self) -> str:
        return self.reasoning_effort or (
            "thinking" if self.thinking_enabled else ""
        )


class HookConfig(StrictModel):
    stage: str
    target: str
    base_dir: Path | None = Field(default=None, exclude=True)

    @field_validator("stage")
    @classmethod
    def _validate_stage(cls, value: str) -> str:
        HookStage(value)
        return value

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
    config: dict[str, Any] = Field(default_factory=dict)


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
    access: Literal["allow", "readwrite", "readonly", "deny", "ask"] = "readonly"


class SandboxConfig(StrictModel):
    enabled: bool = True
    network: bool = True
    external_read: Literal["allow", "readwrite", "readonly", "deny", "ask"] = "ask"
    external_write: Literal["allow", "readwrite", "readonly", "deny", "ask"] = "deny"
    workspace_read: Literal["allow", "readwrite", "readonly", "deny", "ask"] = "allow"
    workspace_write: Literal["allow", "readwrite", "readonly", "deny", "ask"] = "allow"
    resources: list[SandboxResourceConfig] = Field(default_factory=list)


class ToolResultConfig(StrictModel):
    max_inline_chars: int = Field(default=12_000, ge=1)
    preview_chars: int = Field(default=4_000, ge=0)

    @model_validator(mode="after")
    def _validate_preview(self) -> "ToolResultConfig":
        if self.preview_chars > self.max_inline_chars:
            raise ValueError("preview_chars cannot exceed max_inline_chars")
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

    @property
    def plugin_configs(self) -> dict[str, dict[str, Any]]:
        return {
            name: entry.config
            for name, entry in self.plugins.items()
            if entry.enabled
        }

    @property
    def disabled_plugins(self) -> list[str]:
        return [name for name, entry in self.plugins.items() if not entry.enabled]

def config_dict(value: BaseModel | dict[str, Any] | None) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, BaseModel):
        return value.model_dump(exclude_none=True)
    return dict(value)


__all__ = [
    "ConfigOverlay",
    "HookConfig",
    "PermissionConfig",
    "PermissionRuleConfig",
    "PluginConfig",
    "ProviderConfig",
    "SandboxConfig",
    "RuntimeConfig",
    "ToolResultConfig",
    "UserContext",
    "WorkspaceToolConfig",
    "config_dict",
]
