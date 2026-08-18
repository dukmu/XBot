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

import asyncio
import importlib
import inspect
import os
import re
import sys
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from xcore import Context, FiberState

logger = logging.getLogger("loader")


class LoadError(RuntimeError):
    """A tree entry failed to load or did not activate."""


@dataclass
class PluginEntry:
    """One configured plugin node in the tree (aligned with EntryOptions)."""

    id: str
    name: str
    config: dict[str, Any] = field(default_factory=dict)
    disabled: bool = False
    inject: dict[str, Any] | list[str] | None = None
    isolate: dict[str, Any] | None = None


_MISSING = object()


def _lookup(values: dict[str, Any] | None, ref: str) -> Any:
    """Resolve ``a.b.c`` against a values mapping (set memberships allowed).

    Returns :data:`_MISSING` when a key does not exist (so an existing key
    with a ``None`` value stays resolvable).
    """
    if values is None:
        return _MISSING
    parts = ref.split(".")
    target: Any = values
    for index, part in enumerate(parts):
        if isinstance(target, dict):
            if part not in target:
                return _MISSING
            target = target[part]
        elif isinstance(target, (set, frozenset)):
            if index != len(parts) - 1:
                return _MISSING
            return part in target
        elif isinstance(target, (list, tuple)) and part.isdigit():
            position = int(part)
            if position >= len(target):
                return _MISSING
            target = target[position]
        else:
            return _MISSING
    return target


def _resolve_ref(value: Any, values: dict[str, Any] | None) -> Any:
    """Resolve ``${name}`` / ``${env:VAR}`` references in tree config.

    Mirrors DeepSeek Harness's ``!!js`` expressions in cordis.patch.yml: the
    tree is fully declarative and the composition root supplies the dynamic
    session values (paths, state store, provider, child-engine factory, ...)
    as a plain mapping.  ``${env:NAME}`` reads the process environment.
    """
    if values is None:
        return value
    if isinstance(value, str):
        match = re.fullmatch(r"\$\{([^}]+)\}", value)
        if match:
            ref = match.group(1)
            if ref.startswith("env:"):
                return os.environ.get(ref[4:], "")
            resolved = _lookup(values, ref)
            if resolved is _MISSING:
                # Unknown references stay literal: runtime variables such as
                # ${workspace} are expanded by the consuming service.
                return value
            return resolved
        return value
    if isinstance(value, dict):
        return {key: _resolve_ref(item, values) for key, item in value.items()}
    if isinstance(value, list):
        return [_resolve_ref(item, values) for item in value]
    return value


def _entry_from_dict(
    data: dict[str, Any], values: dict[str, Any] | None = None
) -> PluginEntry:
    resolved_config = _resolve_ref(data.get("config") or {}, values)
    if not isinstance(resolved_config, dict):
        # A bare ${...} reference without runtime values stays literal;
        # treat it as the default (no config).
        resolved_config = {}
    entry = PluginEntry(
        id=str(data.get("id") or data.get("name")),
        name=str(data["name"]),
        config=dict(resolved_config or {}),
        disabled=bool(_resolve_ref(data.get("disabled", False), values)),
        inject=_resolve_ref(data.get("inject"), values),
        isolate=_resolve_ref(data.get("isolate"), values),
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
    def from_yaml(
        cls, path: Path | str, values: dict[str, Any] | None = None
    ) -> "PluginTree":
        with open(path, encoding="utf-8") as stream:
            data = yaml.safe_load(stream)
        if isinstance(data, dict):
            raw = data.get("plugins") or data.get("entries") or []
        elif isinstance(data, list):
            raw = data
        else:
            raw = []
        return cls([_entry_from_dict(item, values) for item in raw])

    def merged_with(self, other: "PluginTree") -> "PluginTree":
        """Later tree overrides entries with the same id; others append.

        The later entry's ``config`` is deep-merged into the base entry's
        config (so an overlay can patch one field without restating the
        session-dynamic values); ``disabled`` / ``inject`` / ``isolate`` are
        replaced by the later entry.
        """
        merged: dict[str, PluginEntry] = {entry.id: entry for entry in self.entries}
        for entry in other.entries:
            existing = merged.get(entry.id)
            if existing is not None:
                entry = PluginEntry(
                    id=entry.id,
                    name=entry.name,
                    config=_merge_config(existing.config, entry.config),
                    disabled=entry.disabled,
                    inject=entry.inject if entry.inject is not None else existing.inject,
                    isolate=entry.isolate if entry.isolate is not None else existing.isolate,
                )
            merged[entry.id] = entry
        return PluginTree(list(merged.values()))

    def excluding(self, entry_ids: set[str]) -> "PluginTree":
        """Return a tree without the given entry ids."""
        return PluginTree(
            [entry for entry in self.entries if entry.id not in entry_ids]
        )


def _merge_config(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in overlay.items():
        current = merged.get(key)
        merged[key] = (
            _merge_config(current, value)
            if isinstance(current, dict) and isinstance(value, dict)
            else value
        )
    return merged


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
        """Mount every enabled entry; activation is service-availability driven.

        Row order in the tree is not load semantics: every entry is mounted
        (declaring its ``inject`` dependencies), XCore activates each fiber as
        its required services become available, and this method waits for all
        fibers to converge.  An entry that fails or stays pending on unmet
        dependencies raises a loader error naming it (Cordis/DSH parity:
        "Row order carries no load semantics").
        """
        if not self.ctx.is_active:
            await self.ctx.start()
        mounted_ids: list[str] = []
        for entry in self.tree.entries:
            if entry.disabled:
                logger.info("loader: entry %s disabled, skipping", entry.id)
                continue
            self._mount_handle(entry)
            mounted_ids.append(entry.id)
        # Activation is event-driven: each round drives every unsettled fiber
        # (current handles — reloads replace them) and yields so the settle
        # loops and service-provided events advance.  Dependency chains are
        # shallow (<= ~6 layers), so a fixed round cap is deterministic; a
        # plugin still pending afterwards has unmet inject dependencies.
        for _round in range(16):
            awaiting = [
                handle
                for handle in self._handles.values()
                if handle.state
                not in (FiberState.RUNNING, FiberState.FAILED)
            ]
            if not awaiting:
                break
            await asyncio.gather(*awaiting)
            await asyncio.sleep(0)
        for entry_id in mounted_ids:
            handle = self._handles.get(entry_id)
            if handle is None:
                continue
            if handle.state is FiberState.FAILED:
                fiber = handle._fiber
                raise LoadError(
                    f"plugin {entry_id!r} failed to load: {fiber._error}"
                )
            if handle.state is not FiberState.RUNNING:
                fiber = handle._fiber
                missing = sorted(
                    name for name, required in fiber.inject.items() if required
                )
                raise LoadError(
                    f"plugin {entry_id!r} did not activate; "
                    f"unmet inject dependencies: {missing}"
                )

    def _mount_handle(self, entry: PluginEntry) -> Any:
        """Import, resolve, and mount one entry; returns its PluginHandle."""
        try:
            module = importlib.import_module(f"XBotv2.{entry.name}.plugin")
        except ModuleNotFoundError:
            try:
                module = importlib.import_module(f"{entry.name}.plugin")
            except ModuleNotFoundError:
                try:
                    module = importlib.import_module(f"XBotv2.{entry.name}")
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
        self._handles[entry.id] = handle
        self._plugins[entry.id] = plugin
        logger.info("loader: mounted entry %s (%s)", entry.id, entry.name)
        return handle

    async def _mount(self, entry: PluginEntry) -> None:
        """Mount one entry and wait for it to converge (reload path)."""
        handle = self._mount_handle(entry)
        await handle
        if handle.state is not FiberState.RUNNING:
            raise LoadError(f"plugin {entry.id!r} did not activate")

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
        names = {
            entry.name,
            f"{entry.name}.plugin",
            f"XBotv2.{entry.name}",
            f"XBotv2.{entry.name}.plugin",
        }
        for stale in list(sys.modules):
            if any(
                stale == name or stale.startswith(f"{name}.")
                for name in names
            ):
                sys.modules.pop(stale, None)
        await self.unload(entry_id)
        try:
            await self._mount(entry)
        except BaseException:
            logger.exception("loader: reload of entry %s failed", entry_id)
            return False
        return entry_id in self._handles

    async def apply_patch(self, patch: "PluginTree") -> list[str]:
        """Merge a tree patch and (re)apply the affected entries.

        Used by the workspace_instructions plugin to apply a workspace
        ``.xbot/plugins.yaml`` overlay after the tree is loaded: entries the
        patch overrides are reloaded with their new config; new ids are
        mounted; entries the patch disables are unloaded.  The caller (the
        patch-applying plugin itself) is skipped so it does not reload itself.
        """
        self.tree = self.tree.merged_with(patch)
        affected: list[str] = []
        for entry in patch.entries:
            if entry.id == getattr(self, "_patch_owner", None):
                continue
            if entry.disabled:
                if entry.id in self._handles:
                    await self.unload(entry.id)
            elif entry.id in self._handles:
                await self.reload(entry.id)
            else:
                await self._mount(entry)
            affected.append(entry.id)
        return affected

    def patch_from_path(self, path: Path) -> "PluginTree":
        """Parse a yaml patch file without touching the tree."""
        return PluginTree.from_yaml(path)

    async def apply_patch_path(self, path: Path) -> list[str]:
        """Load a yaml patch file and apply it (see :meth:`apply_patch`)."""
        return await self.apply_patch(self.patch_from_path(path))

    async def unload_all(self) -> list[str]:
        unloaded = []
        for entry_id in reversed(list(self._handles)):
            if await self.unload(entry_id):
                unloaded.append(entry_id)
        return unloaded


__all__ = ["LoadError", "Loader", "LoaderComponent", "PluginEntry", "PluginTree", "resolve_plugin_from_module"]


class LoaderComponent:
    """Mount the loader as a component: provides ``ctx.loader``."""

    name = "xbot.loader"

    def __init__(self, tree: PluginTree) -> None:
        self._tree = tree

    def apply(self, ctx: Context, config: Any = None) -> None:
        loader = Loader(ctx, tree=self._tree)
        ctx.set("loader", loader)
        # XCore owns application teardown. Keep the loader's bookkeeping in
        # sync when its provider fiber is stopped; each child handle remains
        # idempotently disposable even if XCore already stopped it first.
        ctx.dispose(loader.unload_all)


class _MountAdapter:
    """Expose a plugin object to XCore's plugin system.

    XCore tracks the currently applying fiber (:func:`xcore.current_fiber`),
    so capability services (``ctx.tools`` etc.) attach fiber-scoped cleanup
    themselves — no caller-tracking here.  Plugin metadata (``name`` /
    ``Config`` / ``inject``) is passed through for XCore resolution.
    """

    def __init__(self, plugin: Any) -> None:
        self._plugin = plugin
        self.name = getattr(plugin, "name", None) or type(plugin).__name__
        self.Config = getattr(plugin, "Config", None)
        self.inject = getattr(plugin, "inject", None)

    async def apply(self, ctx: Context, config: Any) -> Any:
        result = self._plugin.apply(ctx, config)
        if inspect.isawaitable(result):
            result = await result
        return result
