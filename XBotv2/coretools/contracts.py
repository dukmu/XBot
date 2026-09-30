"""Typed configuration owned by the core-tools plugin."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, PositiveFloat, field_validator
from XBotv2.agentloop.contracts import AllTools, ToolSelection


class CoreToolsModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


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


class CoreToolsConfig(CoreToolsModel):
    shell_max_timeout_seconds: PositiveFloat = 120.0
    tool_timeout_seconds: PositiveFloat = 60.0
    tool_timeouts: dict[str, PositiveFloat] = Field(default_factory=dict)
    enabled_tools: ToolSelection = Field(default_factory=AllTools)
    hooks: list[HookConfig] = Field(default_factory=list)
    workspace_tools: list[WorkspaceToolConfig] = Field(default_factory=list)


__all__ = [
    "CoreToolsConfig",
    "HookConfig",
    "WorkspaceToolConfig",
]
