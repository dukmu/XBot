"""Typed configuration owned by the core-tools plugin."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class CoreToolsModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class HookConfig(CoreToolsModel):
    stage: str
    target: str

    @field_validator("target")
    @classmethod
    def validate_target(cls, value: str) -> str:
        source, separator, handler = value.partition(":")
        if not separator or not source or not handler:
            raise ValueError("target must use source:handler syntax")
        return value


class WorkspaceToolConfig(CoreToolsModel):
    """One explicit Tool export from the workspace's ``.xbot/tools`` directory."""

    target: str

    @field_validator("target")
    @classmethod
    def validate_target(cls, value: str) -> str:
        source, separator, export = value.partition(":")
        if not separator or not source or not export:
            raise ValueError("target must use tools/module.py:export syntax")
        return value


class ToolResultConfig(CoreToolsModel):
    cache_threshold_chars: int = Field(default=12_000, ge=1)
    preview_chars: int = Field(default=8_000, ge=0)
    tail_chars: int = Field(default=2_000, ge=0)

    @model_validator(mode="after")
    def validate_preview(self) -> "ToolResultConfig":
        if self.preview_chars > self.cache_threshold_chars:
            raise ValueError("preview_chars cannot exceed cache_threshold_chars")
        if self.tail_chars > self.preview_chars:
            raise ValueError("tail_chars cannot exceed preview_chars")
        return self


class CoreToolsConfig(CoreToolsModel):
    tool_results: ToolResultConfig = Field(default_factory=ToolResultConfig)
    tools: list[str] | None = None
    hooks: list[HookConfig] = Field(default_factory=list)
    workspace_tools: list[WorkspaceToolConfig] = Field(default_factory=list)


__all__ = [
    "CoreToolsConfig",
    "HookConfig",
    "ToolResultConfig",
    "WorkspaceToolConfig",
]
