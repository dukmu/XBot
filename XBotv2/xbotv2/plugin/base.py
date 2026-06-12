"""Plugin base class — all plugins inherit from this."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from xbotv2.hooks.manager import HookManager
    from xbotv2.plugin.manifest import PluginManifest
    from xbotv2.plugin.store import PluginStore
    from xbotv2.tools.registry import ToolRegistry


class PluginBase(ABC):
    """Base class for all XBotv2 plugins.

    Plugins extend this class to declare hooks, tools, and prompt fragments.
    The plugin system calls these methods during initialization to wire
    everything into the engine.

    Each plugin instance gets its own ``store`` (a dict-like namespace in
    the global state) that core never touches.
    """

    def __init__(self, manifest: "PluginManifest", store: "PluginStore") -> None:
        self.manifest = manifest
        self.store = store
        self._config: dict[str, Any] = {}

    # ------------------------------------------------------------------
    # Lifecycle (called by loader during bootstrap)
    # ------------------------------------------------------------------

    @abstractmethod
    async def on_load(self, config: dict[str, Any]) -> None:
        """Called when plugin is loaded. Validate config, init resources."""
        ...

    async def on_unload(self) -> None:
        """Called when plugin is unloaded. Clean up resources."""
        pass

    # ------------------------------------------------------------------
    # Hook registration
    # ------------------------------------------------------------------

    def register_hooks(self, manager: "HookManager") -> None:
        """Register this plugin's hooks with the HookManager.

        Default implementation uses the manifest declarations.
        Plugins can override for dynamic hook registration.
        """
        from xbotv2.hooks.types import HookStage

        for decl in self.manifest.hooks:
            stage = HookStage(decl.stage)
            handler = self._resolve_handler(decl.handler)
            manager.register(stage, handler)

    # ------------------------------------------------------------------
    # Tool registration
    # ------------------------------------------------------------------

    def register_tools(self, registry: "ToolRegistry") -> None:
        """Register this plugin's tools with the ToolRegistry."""
        for decl in self.manifest.tools:
            tool = self._resolve_handler(decl.handler)
            registry.register(
                tool,
                sandbox_mode=decl.sandbox_mode,
                execution_mode=decl.execution_mode,
                lock_fields=tuple(decl.lock_fields),
                owner_plugin=self.manifest.name,
            )

    # ------------------------------------------------------------------
    # Prompt fragments
    # ------------------------------------------------------------------

    def get_prompt_fragments(self) -> dict[str, str]:
        """Return rendered prompt fragments keyed by injection stage.

        Returns:
            dict mapping fragment stage ("system_instructions", "dag_suffix",
            "system_prefix", "system_rules") to rendered text.
        """
        fragments: dict[str, str] = {}

        for decl in self.manifest.prompt_fragments:
            if decl.handler:
                handler = self._resolve_handler(decl.handler)
                fragments[decl.stage] = handler()
            elif decl.file:
                from pathlib import Path

                plugin_dir = self.manifest.plugin_dir or Path.cwd()
                file_path = plugin_dir / decl.file
                if not file_path.exists():
                    raise FileNotFoundError(
                        f"Plugin '{self.manifest.name}' prompt fragment file not found: {file_path}"
                    )
                with open(file_path) as f:
                    fragments[decl.stage] = f.read()

        return fragments

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _resolve_handler(dotted_path: str) -> Any:
        """Resolve 'module.submodule:function_name' to a callable.

        For builtin plugins the path is relative to the plugin package.
        For example 'planning.hooks:on_init' resolves the ``on_init``
        function from ``builtin_plugins.planning.hooks``.
        """
        import importlib

        module_path, _, attr = dotted_path.partition(":")
        if not attr:
            raise ValueError(f"Invalid handler path (missing ':attr'): {dotted_path!r}")

        module = importlib.import_module(module_path)
        return getattr(module, attr)
