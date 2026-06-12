"""Configuration data models for XBotv2."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


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

    stage: str
    target: str  # "module:function"


class SystemConfig(BaseModel):
    """Runtime configuration from config/system.yaml plus AGENTS.md."""

    agent_name: str = Field(default="XBotv2")
    agent_role: str = Field(default="AI coding assistant")
    system_prompt: str = Field(default="You are a helpful AI assistant.")
    provider: str = Field(default="default")
    max_context_tokens: int = Field(default=32000)
    tools: list[str] = Field(default_factory=list)
    hooks: list[HookConfig] = Field(default_factory=list)
    plugins: dict[str, dict] = Field(default_factory=dict)
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
