"""Plugin tree loader: cordis.yaml-style declarative plugin mounting.

Reference model: DeepSeek Harness's Cordis loader
(``@cordisjs/plugin-loader`` + ``@cordisjs/plugin-include``).  The runtime is
declared as a tree of plugin *entries* (id / module name / config / disabled /
inject / isolate); a loader service imports each module, resolves the plugin
it exports, and mounts it on the XCore context (optionally on an isolated
scope).  Dependencies are expressed with XCore's ``inject`` (services) and the
tree's load order -- there is no separate plugin system beside XCore's.
"""

from __future__ import annotations

import importlib
import inspect
import sys
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from xcore import Context

logger = logging.getLogger("loader")


@dataclass
class PluginEntry:
    """One configured plugin node in the tree (aligned with EntryOptions)."""

    id: str
    name: str
    config: dict[str, Any] = field(default_factory=dict)
    disabled: bool = False
    inject: dict[str, Any] | list[str] | None = None
    isolate: dict[str, Any] | None = None


def _entry_from_dict(data: dict[str, Any]) -> PluginEntry:
    entry = PluginEntry(
        id=str(data.get("id") or data.get("name")),
        name=str(data["name"]),
        config=dict(data.get("config") or {}),
        disabled=bool(data.get("disabled", False)),
        inject=data.get("inject"),
        isolate=data.get("isolate"),
    )
    if not entry.id:
        raise ValueError("plugin tree entry requires an id or name")
    return entry


class PluginTree:
    """Ordered list of plugin entries (from a dict, YAML file, or entries)."""

    def __init__(self, entries: list[PluginEntry]) -> None:
        seen: set[str] = set()
        for entry in entries:
            if entry.id in seen:
                raise ValueError(f"duplicate plugin tree entry id: {entry.id}")
            seen.add(entry.id)
        self.entries = list(entries)

    @classmethod
    def from_dict(cls, data: Any) -> "PluginTree":
        if data is None:
            return cls([])
        if isinstance(data, dict):
            raw = data.get("plugins") or data.get("entries") or []
        elif isinstance(data, list):
            raw = data
        else:
            raise TypeError("plugin tree must be a list of entries or {plugins: [...]}")
        if not isinstance(raw, list):
            raise TypeError("plugin tree entries must be a list")
        return cls([_entry_from_dict(item) for item in raw])

    @classmethod
    def from_yaml(cls, path: Path | str) -> "PluginTree":
        with open(path, encoding="utf-8") as stream:
            data = yaml.safe_load(stream)
        return cls.from_dict(data)

    def merged_with(self, other: "PluginTree") -> "PluginTree":
        """Later tree overrides entries with the same id; others append."""
        merged = {entry.id: entry for entry in self.entries}
        for entry in other.entries:
            merged[entry.id] = entry
        return PluginTree(list(merged.values()))


def resolve_plugin_from_module(module: Any, name: str) -> Any:
    """Extract the plugin exported by a module.

    Convention: a package ``<name>`` (or module ``<name>.plugin``) exports
    ``plugin`` (a plugin object/function) or a ``Plugin`` class; the module
    itself may also be a plugin (has ``apply``).  Package attributes are
    followed into submodules.
    """
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
    """Mount tree entries as XCore plugins (registered as ``ctx.loader``)."""

    def __init__(self, ctx: Context, *, tree: PluginTree) -> None:
        self.ctx = ctx
        self.tree = tree
        self._handles: dict[str, Any] = {}
        self._plugins: dict[str, Any] = {}

    # -- inspection ---------------------------------------------------------

    @property
    def commands(self) -> tuple[Any, ...]:
        """All registered commands (including dynamic ones)."""
        return self.ctx.commands.all()

    def get_command(self, name: str) -> Any | None:
        return self.ctx.commands.get(name)

    def get(self, entry_id: str) -> Any | None:
        """Return the mounted plugin instance/object for an entry id."""
        return self._plugins.get(entry_id)

    def handle(self, entry_id: str) -> Any | None:
        return self._handles.get(entry_id)

    @property
    def loaded_ids(self) -> tuple[str, ...]:
        return tuple(self._handles)

    async def status_slots(self) -> dict[str, str]:
        """Collect validated status slots without exposing plugin objects."""
        slots: dict[str, str] = {}
        for plugin in self._plugins.values():
            status_slots = getattr(plugin, "status_slots", None)
            if status_slots is None:
                continue
            try:
                values = await status_slots()
            except Exception:
                logger.exception(
                    "Plugin %s status slots failed", getattr(plugin, "name", "?")
                )
                continue
            if not isinstance(values, dict):
                continue
            for raw_name, raw_value in values.items():
                name = str(raw_name).strip()
                value = str(raw_value).strip()
                if name and value and name not in slots:
                    slots[name] = value
        return slots

    # -- mounting -----------------------------------------------------------

    async def load(self) -> None:
        """Mount every enabled entry in tree order (disables skipped).

        Starts the context on first load so mounted entries load immediately
        (the loader owns plugin lifecycle; the app owns teardown through
        :meth:`unload_all`).
        """
        if not self.ctx.is_active:
            await self.ctx.start()
        for entry in self.tree.entries:
            if entry.disabled:
                logger.info("loader: entry %s disabled, skipping", entry.id)
                continue
            await self._mount(entry)

    async def _mount(self, entry: PluginEntry) -> None:
        try:
            module = importlib.import_module(f"{entry.name}.plugin")
        except ModuleNotFoundError:
            module = importlib.import_module(entry.name)
        plugin = resolve_plugin_from_module(module, entry.name)
        if not inspect.isclass(plugin) and not inspect.isfunction(plugin):
            # Module-level object plugins share one instance across mounts;
            # mount a fresh copy so per-app state (e.g. session-init flags)
            # does not leak between applications (the legacy loader
            # instantiated plugins per load).
            try:
                plugin = type(plugin)()
            except TypeError:
                pass
        mount_ctx = self.ctx
        if entry.isolate:
            for name, label in entry.isolate.items():
                mount_ctx = mount_ctx.isolate(name, label if label is not True else None)
        handle = mount_ctx.plugin(_MountAdapter(plugin), entry.config)
        await handle
        self._handles[entry.id] = handle
        self._plugins[entry.id] = plugin
        logger.info("loader: mounted entry %s (%s)", entry.id, entry.name)

    async def unload(self, entry_id: str) -> bool:
        handle = self._handles.pop(entry_id, None)
        if handle is None:
            return False
        self._plugins.pop(entry_id, None)
        await handle.dispose()
        return True

    async def reload(self, entry_id: str) -> bool:
        """Reload one mounted entry (re-imports its module and re-applies it).

        Used by the agent reload endpoint to re-read workspace Agent
        definitions.  Returns whether the entry is mounted after the attempt.
        """
        entry = next(
            (item for item in self.tree.entries if item.id == entry_id),
            None,
        )
        if entry is None or entry_id not in self._handles:
            return False
        for stale in list(sys.modules):
            if stale == entry.name or stale.startswith(f"{entry.name}."):
                sys.modules.pop(stale, None)
        await self.unload(entry_id)
        try:
            await self._mount(entry)
        except BaseException:
            logger.exception("loader: reload of entry %s failed", entry_id)
            return False
        return entry_id in self._handles

    async def unload_all(self) -> list[str]:
        unloaded = []
        for entry_id in reversed(list(self._handles)):
            if await self.unload(entry_id):
                unloaded.append(entry_id)
        return unloaded


__all__ = ["Loader", "LoaderComponent", "PluginEntry", "PluginTree", "resolve_plugin_from_module"]


class LoaderComponent:
    """Mount the loader as a component: provides ``ctx.loader``."""

    name = "xbot.loader"

    def __init__(self, tree: PluginTree) -> None:
        self._tree = tree

    def apply(self, ctx: Context, config: Any = None) -> None:
        ctx.set("loader", Loader(ctx, tree=self._tree))


class _MountAdapter:
    """Expose a plugin with caller-tracking during ``apply``.

    XBot capability services (``ctx.tools`` etc.) attach fiber-scoped cleanup
    based on the currently executing plugin context; the loader sets that
    context for the duration of ``apply`` so registrations are undone on
    unload.  Plugin metadata (``name`` / ``Config`` / ``inject``) is passed
    through for XCore resolution.
    """

    def __init__(self, plugin: Any) -> None:
        self._plugin = plugin
        self.name = getattr(plugin, "name", None) or type(plugin).__name__
        self.Config = getattr(plugin, "Config", None)
        self.inject = getattr(plugin, "inject", None)

    async def apply(self, ctx: Context, config: Any) -> Any:
        from tools.plugin import _active_ctx

        token = _active_ctx.set(ctx)
        try:
            result = self._plugin.apply(ctx, config)
            if inspect.isawaitable(result):
                result = await result
            return result
        finally:
            _active_ctx.reset(token)
