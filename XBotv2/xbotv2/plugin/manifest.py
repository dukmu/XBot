"""Plugin manifest and declaration models."""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class HookDeclaration(BaseModel):
    """A single hook registered by a plugin."""

    stage: str = Field(..., description="HookStage value, e.g. 'before_context'")
    handler: str = Field(..., description="Dotted path, e.g. 'planning.hooks:on_init'")


class ToolDeclaration(BaseModel):
    """A single tool registered by a plugin."""

    handler: str = Field(..., description="Dotted path, e.g. 'planning.tools:plan_add_nodes'")
    sandbox_mode: str = Field(default="host", description="'sandboxed' or 'host'")
    execution_mode: str = Field(default="sequential", description="'parallel' or 'sequential'")
    lock_fields: list[str] = Field(default_factory=list)


class PromptFragmentDeclaration(BaseModel):
    """A prompt fragment injected by a plugin."""

    stage: str = Field(
        ...,
        description="Injection stage: system_prefix, system_instructions, system_rules, dag_suffix",
    )
    file: str | None = Field(default=None, description="Path to .md file relative to plugin root")
    handler: str | None = Field(
        default=None, description="Function path that returns rendered text"
    )


class PluginManifest(BaseModel):
    """Complete plugin manifest (loaded from plugin.yaml)."""

    name: str = Field(..., description="Unique plugin name")
    version: str = Field(..., description="Semver version")
    description: str = Field(default="", description="Human-readable description")
    depends_on: list[str] = Field(default_factory=list, description="Plugin dependencies")
    hooks: list[HookDeclaration] = Field(default_factory=list)
    tools: list[ToolDeclaration] = Field(default_factory=list)
    prompt_fragments: list[PromptFragmentDeclaration] = Field(default_factory=list)
    config_schema: dict | None = Field(
        default=None, description="JSON Schema for plugin config validation"
    )
