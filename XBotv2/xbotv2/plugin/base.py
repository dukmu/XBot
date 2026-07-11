"""Single-entry plugin setup API."""

from __future__ import annotations

import importlib
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TYPE_CHECKING

from xbotv2.hooks.types import HookStage

if TYPE_CHECKING:
    from xbotv2.core.context import ContextBuilder
    from xbotv2.hooks.manager import HookManager
    from xbotv2.plugin.manifest import PluginManifest
    from xbotv2.plugin.store import PluginStore
    from xbotv2.tools.registry import ToolRegistry


@dataclass
class PluginSetupContext:
    """Capability object used for transactional plugin registration."""

    plugin_name: str
    hooks: "HookManager"
    tools: "ToolRegistry"
    context: "ContextBuilder"
    hook_refs: list[tuple[HookStage, Any]] = field(default_factory=list)
    tool_names: list[str] = field(default_factory=list)
    fragment_stages: list[str] = field(default_factory=list)

    def register_hook(self, stage: HookStage, callback: Any) -> None:
        self.hooks.register(stage, callback)
        self.hook_refs.append((stage, callback))

    def register_tool(self, tool: Any, **options: Any) -> str:
        before = set(self.tools.registered_names())
        self.tools.register(tool, owner_plugin=self.plugin_name, **options)
        added = [name for name in self.tools.registered_names() if name not in before]
        self.tool_names.extend(added)
        return added[0]

    def add_prompt_fragment(self, stage: str, text: str) -> None:
        self.context.register_fragment(stage, self.plugin_name, text)
        if stage not in self.fragment_stages:
            self.fragment_stages.append(stage)

    def rollback(self) -> None:
        for stage, callback in reversed(self.hook_refs):
            self.hooks.unregister(stage, callback)
        for tool_name in reversed(self.tool_names):
            self.tools.unregister(tool_name)
        for stage in reversed(self.fragment_stages):
            self.context.unregister_fragment(stage, self.plugin_name)


class PluginBase(ABC):
    """Base class for XBot plugin API v1."""

    def __init__(self, manifest: "PluginManifest", store: "PluginStore") -> None:
        self.manifest = manifest
        self.store = store
        self._config: dict[str, Any] = {}

    @abstractmethod
    async def on_load(self, config: dict[str, Any]) -> None:
        """Validate configuration and initialize external resources."""
        ...

    async def on_unload(self) -> None:
        """Release resources created by on_load."""

    def setup(self, ctx: PluginSetupContext) -> None:
        """Register manifest-declared extensions through one transaction."""
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
            ctx.add_prompt_fragment(
                declaration.stage,
                self._render_fragment(declaration),
            )

    def diagnostics(self) -> dict[str, Any]:
        return {"status": "ready"}

    def _render_fragment(self, declaration: Any) -> str:
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
            raise ValueError(f"Invalid handler path (expected module:attribute): {dotted_path!r}")
        module = importlib.import_module(module_path)
        return getattr(module, attribute)


__all__ = ["PluginBase", "PluginSetupContext"]
