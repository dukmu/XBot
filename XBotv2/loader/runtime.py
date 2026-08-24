"""Load one resolved plugin tree into XCore."""

from __future__ import annotations

import importlib
import inspect
import logging
from typing import Any

from xcore import Context, FiberState, PluginHandle

from XBotv2.loader.types import LoadError, PluginEntry, PluginTree

logger = logging.getLogger("loader")


def resolve_plugin_from_module(module: Any, name: str) -> Any:
    """Resolve the conventional plugin export from an imported module."""
    candidate = getattr(module, "plugin", None)
    if _is_module(candidate):
        candidate = getattr(candidate, "plugin", None)
    if candidate is not None:
        return candidate
    candidate = getattr(module, "Plugin", None)
    if _is_module(candidate):
        candidate = getattr(candidate, "Plugin", None)
    if candidate is not None:
        return candidate
    if callable(getattr(module, "apply", None)) or callable(module):
        return module
    raise ImportError(
        f"plugin module {name!r} must export 'plugin', 'Plugin', or be a plugin itself"
    )


def _is_module(value: Any) -> bool:
    import types

    return isinstance(value, types.ModuleType)


class Loader:
    """Import, mount, inspect, and unload a fixed startup plugin tree."""

    def __init__(self, ctx: Context, *, tree: PluginTree) -> None:
        self.ctx = ctx
        self.tree = tree
        self._handles: dict[str, PluginHandle] = {}
        self._plugins: dict[str, Any] = {}

    @property
    def commands(self) -> tuple[Any, ...]:
        return self.ctx.commands.all()

    def get_command(self, name: str) -> Any | None:
        return self.ctx.commands.get(name)

    def get(self, entry_id: str) -> Any | None:
        return self._plugins.get(entry_id)

    def handle(self, entry_id: str) -> PluginHandle | None:
        return self._handles.get(entry_id)

    @property
    def loaded_ids(self) -> tuple[str, ...]:
        return tuple(self._handles)

    async def load(self) -> None:
        """Mount all enabled entries and wait for the dependency graph."""
        if not self.ctx.is_active:
            await self.ctx.start()
        mounted_ids: list[str] = []
        for entry in self.tree.entries:
            if entry.disabled:
                logger.info("loader: entry %s disabled, skipping", entry.id)
                continue
            self._mount_handle(entry)
            mounted_ids.append(entry.id)

        await self.ctx.settle()
        for entry_id in mounted_ids:
            handle = self._handles[entry_id]
            if handle.state is FiberState.FAILED:
                assert handle.error is not None
                raise handle.error
            if handle.state is not FiberState.RUNNING:
                raise LoadError(
                    f"plugin {entry_id!r} did not activate; "
                    "unmet inject dependencies: "
                    f"{list(handle.missing_dependencies)}"
                )

    def _mount_handle(self, entry: PluginEntry) -> PluginHandle:
        module = _import_plugin_module(entry.name)
        plugin = resolve_plugin_from_module(module, entry.name)
        if not inspect.isclass(plugin) and not inspect.isfunction(plugin):
            try:
                plugin = type(plugin)()
            except TypeError:
                pass
        mount_ctx = self.ctx
        for name, label in (entry.isolate or {}).items():
            mount_ctx = mount_ctx.isolate(
                name,
                label if label is not True else None,
            )
        handle = mount_ctx.plugin(_MountAdapter(plugin), entry.config)
        self._handles[entry.id] = handle
        self._plugins[entry.id] = plugin
        logger.info("loader: mounted entry %s (%s)", entry.id, entry.name)
        return handle

    async def unload(self, entry_id: str) -> bool:
        handle = self._handles.pop(entry_id, None)
        if handle is None:
            return False
        self._plugins.pop(entry_id, None)
        await handle.dispose()
        return True

    async def unload_all(self) -> list[str]:
        unloaded: list[str] = []
        for entry_id in reversed(tuple(self._handles)):
            if await self.unload(entry_id):
                unloaded.append(entry_id)
        return unloaded


def _import_plugin_module(name: str) -> Any:
    candidates = (
        f"XBotv2.{name}.plugin",
        f"{name}.plugin",
        f"XBotv2.{name}",
        name,
    )
    for candidate in candidates:
        try:
            return importlib.import_module(candidate)
        except ModuleNotFoundError as error:
            if error.name != candidate and not candidate.startswith(
                f"{error.name}."
            ):
                raise
    raise ModuleNotFoundError(name)


class LoaderComponent:
    """Provide the startup tree loader as ``ctx.loader``."""

    name = "xbot.loader"

    def __init__(self, tree: PluginTree) -> None:
        self._tree = tree

    def apply(self, ctx: Context, config: object | None = None) -> None:
        loader = Loader(ctx, tree=self._tree)
        ctx.set("loader", loader)
        ctx.dispose(loader.unload_all)


class _MountAdapter:
    """Pass plugin metadata and application through to XCore."""

    def __init__(self, plugin: Any) -> None:
        self._plugin = plugin
        self.name = getattr(plugin, "name", None) or type(plugin).__name__
        self.Config = getattr(plugin, "Config", None)
        self.inject = getattr(plugin, "inject", None)

    async def apply(self, ctx: Context, config: Any) -> Any:
        result = self._plugin.apply(ctx, config)
        if inspect.isawaitable(result):
            return await result
        return result


__all__ = ["Loader", "LoaderComponent", "resolve_plugin_from_module"]
