"""Configuration data models for XBotv2."""

from __future__ import annotations

from pydantic import BaseModel, Field


class UserContext(BaseModel):
    """User identity from user.yaml."""

    user_id: str = Field(default="default-user")
    user_name: str = Field(default="User")
    platform: str = Field(default="terminal")
    session_type: str = Field(default="interactive")


class ProviderConfig(BaseModel):
    """LLM provider configuration from provider.yaml."""

    provider: str = Field(default="openai")
    model: str = Field(default="gpt-4")
    base_url: str | None = Field(default=None)
    api_key: str | None = Field(default=None)
    temperature: float = Field(default=0.7)
    max_tokens: int = Field(default=4096)


class HookConfig(BaseModel):
    """A personality-configured hook."""

    stage: str
    target: str  # "module:function"


class AgentConfig(BaseModel):
    """Personality configuration from personality.yaml."""

    agent_name: str = Field(default="XBotv2")
    agent_role: str = Field(default="You are a helpful AI assistant.")
    provider: str = Field(default="default")
    max_context_tokens: int = Field(default=32000)
    tools: list[str] = Field(default_factory=list)
    hooks: list[HookConfig] = Field(default_factory=list)
    plugins: dict[str, dict] = Field(default_factory=dict)
    system_template: str = Field(default="")
    instructions: str = Field(default="")
    memory: str = Field(default="")
    sandbox: dict = Field(default_factory=dict)
    permissions: dict = Field(default_factory=dict)
