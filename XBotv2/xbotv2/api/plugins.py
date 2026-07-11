"""Stable contracts for XBotv2 plugins."""

from __future__ import annotations

import importlib
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field

from xbotv2.api.hooks import HookStage
from xbotv2.api.tools import Tool


class HookDeclaration(BaseModel):
    stage: str
    handler: str


class ToolDeclaration(BaseModel):
    handler: str
    sandbox_mode: str = "host"
    execution_mode: str = "sequential"
    lock_fields: list[str] = Field(default_factory=list)


class PromptFragmentDeclaration(BaseModel):
    stage: str
    file: str | None = None
    handler: str | None = None


class PluginManifest(BaseModel):
    """Validated contents of a plugin's ``plugin.yaml`` file."""

    name: str
    version: str
    description: str = ""
    api_version: Literal["1"] = "1"
    depends_on: list[str] = Field(default_factory=list)
    hooks: list[HookDeclaration] = Field(default_factory=list)
    tools: list[ToolDeclaration] = Field(default_factory=list)
    prompt_fragments: list[PromptFragmentDeclaration] = Field(default_factory=list)
    config_schema: dict[str, Any] | None = None
    plugin_dir: Path | None = Field(default=None, exclude=True)


class PluginStore(Protocol):
    """Per-plugin persistent key-value storage."""

    async def get(self, key: str, default: Any = None) -> Any: ...
    async def set(self, key: str, value: Any) -> None: ...
    async def delete(self, key: str) -> None: ...
    async def all(self) -> dict[str, Any]: ...
    async def clear(self) -> None: ...


class PluginSetupContext(Protocol):
    """Capabilities available while a plugin registers extensions."""

    def register_hook(self, stage: HookStage, callback: Any) -> None: ...
    def register_tool(self, tool: Tool, **options: Any) -> str: ...
    def add_prompt_fragment(self, stage: str, text: str) -> None: ...


class PluginBase(ABC):
    """Base class for plugin API version 1."""

    def __init__(self, manifest: PluginManifest, store: PluginStore) -> None:
        self.manifest = manifest
        self.store = store

    @abstractmethod
    async def on_load(self, config: dict[str, Any]) -> None:
        """Validate configuration and initialize external resources."""

    async def on_unload(self) -> None:
        """Release resources created by ``on_load``."""

    def setup(self, ctx: PluginSetupContext) -> None:
        """Register extensions declared by the manifest."""
        for declaration in self.manifest.hooks:
            ctx.register_hook(
                HookStage(declaration.stage),
                self._resolve_handler(declaration.handler),
            )
        for declaration in self.manifest.tools:
            ctx.register_tool(
                self._resolve_handler(declaration.handler),
                sandbox_mode=declaration.sandbox_mode,
                execution_mode=declaration.execution_mode,
                lock_fields=tuple(declaration.lock_fields),
            )
        for declaration in self.manifest.prompt_fragments:
            ctx.add_prompt_fragment(declaration.stage, self._render_fragment(declaration))

    def diagnostics(self) -> dict[str, Any]:
        return {"status": "ready"}

    def _render_fragment(self, declaration: PromptFragmentDeclaration) -> str:
        if declaration.handler:
            handler = self._resolve_handler(declaration.handler)
            return handler() if callable(handler) else str(handler)
        if declaration.file:
            path = (self.manifest.plugin_dir or Path.cwd()) / declaration.file
            if not path.exists():
                raise FileNotFoundError(
                    f"Plugin '{self.manifest.name}' prompt fragment file not found: {path}"
                )
            return path.read_text(encoding="utf-8")
        raise ValueError("Prompt fragment requires either handler or file")

    @staticmethod
    def _resolve_handler(dotted_path: str) -> Any:
        module_path, separator, attribute = dotted_path.partition(":")
        if not separator or not attribute:
            raise ValueError(
                f"Invalid handler path (expected module:attribute): {dotted_path!r}"
            )
        return getattr(importlib.import_module(module_path), attribute)


__all__ = [
    "HookDeclaration",
    "PluginBase",
    "PluginManifest",
    "PluginSetupContext",
    "PluginStore",
    "PromptFragmentDeclaration",
    "ToolDeclaration",
]
