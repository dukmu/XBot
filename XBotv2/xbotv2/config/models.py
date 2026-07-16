"""Configuration data models for XBotv2."""

from __future__ import annotations

from typing import Any

from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator

from xbotv2.api.hooks import HookStage


class UserContext(BaseModel):
    """User identity from user.yaml."""

    user_id: str = Field(default="default-user")
    user_name: str = Field(default="User")
    platform: str = Field(default="terminal")
    session_type: str = Field(default="interactive")


class ProviderConfig(BaseModel):
    """LLM provider configuration from providers.yaml."""

    provider: str = Field(default="openai")
    model: str = Field(default="gpt-4")
    base_url: str | None = Field(default=None)
    api_key: str | None = Field(default=None)
    temperature: float = Field(default=0.7)
    max_tokens: int = Field(default=4096)
    mock_responses: list[dict[str, Any]] = Field(default_factory=list)


class HookConfig(BaseModel):
    """A system-configured hook."""

    model_config = ConfigDict(extra="forbid")

    stage: str
    target: str  # "module:function"
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


class WorkspacePluginConfig(BaseModel):
    """One plugin entry in workspace .xbot/plugins.yaml."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    config: dict[str, Any] = Field(default_factory=dict)


class SystemConfig(BaseModel):
    """Runtime configuration after startup-only workspace overlays."""

    agent_name: str = Field(default="XBotv2")
    agent_role: str = Field(default="AI coding assistant")
    system_prompt: str = Field(default="You are a helpful AI assistant.")
    provider: str = Field(default="default")
    max_context_tokens: int = Field(default=32000)
    max_subagent_depth: int = Field(default=3, ge=1)
    max_concurrent_subagents: int = Field(default=4, ge=1)
    tools: list[str] = Field(default_factory=list)
    hooks: list[HookConfig] = Field(default_factory=list)
    plugins: dict[str, dict] = Field(default_factory=dict)
    plugin_paths: list[str] = Field(default_factory=list)
    disabled_plugins: list[str] = Field(default_factory=list)
    system_template: str = Field(default="")
    instructions: str = Field(default="")
    memory: str = Field(default="")
    sandbox: dict = Field(default_factory=lambda: {
        "enabled": True,
        "external_read": "ask",
        "external_write": "deny",
        "workspace_read": "allow",
        "workspace_write": "allow",
    })
    permissions: dict = Field(default_factory=lambda: {
        "ask": [{"tool": ".*"}],
    })

    @property
    def effective_instructions(self) -> str:
        parts = [self.system_prompt, self.instructions]
        return "\n\n".join(part for part in parts if part.strip())
