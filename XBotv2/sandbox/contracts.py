"""Public sandbox capability and configuration contracts."""

from __future__ import annotations

from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field
from pydantic import JsonValue


PathAccess = Literal["allow", "readwrite", "readonly", "deny"]


class SandboxResourceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    access: PathAccess = "readonly"


class SandboxConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    network: bool = True
    external_read: PathAccess = "readonly"
    external_write: PathAccess = "deny"
    workspace_read: PathAccess = "allow"
    workspace_write: PathAccess = "allow"
    resources: list[SandboxResourceConfig] = Field(default_factory=list)


class SandboxPort(Protocol):
    enabled: bool
    network: bool

    async def filesystem(
        self,
        operation: str,
        args: dict[str, JsonValue],
    ) -> str: ...

    def resolve_filesystem_args(
        self,
        operation: str,
        args: dict[str, JsonValue],
    ) -> dict[str, JsonValue]: ...


__all__ = [
    "PathAccess",
    "SandboxConfig",
    "SandboxPort",
    "SandboxResourceConfig",
]
