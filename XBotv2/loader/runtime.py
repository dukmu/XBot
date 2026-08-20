"""Concrete XCore plugin-tree loader implementation."""

from __future__ import annotations

import asyncio
import importlib
import inspect
import logging
import sys
from pathlib import Path
from typing import Any

from xcore import Context, FiberState

from XBotv2.agentloop import Events
from XBotv2.loader.types import LoadError, PluginEntry, PluginTree

logger = logging.getLogger("loader")


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

    async def apply_external_layer(
        self,
        path: Path,
        values: dict[str, Any],
    ) -> tuple[list[str], list[str]]:
        """Re-apply one external plugin-tree layer to the live tree.

        Generic soft-restart delta: reload changed entries, mount new ones,
        unload disabled ones, and skip entries declared ``reloadable: false``.
        Returns ``(reloaded_ids, notices)``; a failing entry keeps the last
        good binding and is reported instead of aborting the restart.
        """
        patch = _config_layer(path, values)
        reloaded: list[str] = []
        notices: list[str] = []
        for entry in patch.entries:
            if not entry.reloadable:
                notices.append(
                    f"{entry.id}: cannot be hot-reloaded; restart required"
                )
                continue
            try:
                affected = await self.apply_patch(PluginTree([entry]))
                if not entry.disabled and entry.id not in self.loaded_ids:
                    notices.append(f"{entry.id}: reload failed; keeping last good")
                reloaded.extend(affected)
            except Exception as error:  # noqa: BLE001 - keep last good per entry
                notices.append(f"{entry.id}: {error}")
        return reloaded, notices

    async def handle_soft_reload(self, event: Any) -> None:
        """SOFT_RELOAD listener: re-apply the external tree for system scope."""
        payload = event.event if isinstance(event.event, dict) else {}
        if payload.get("scope") != "system":
            return
        path = payload.get("config_path")
        values = payload.get("values")
        if not path or not values:
            return
        reloaded, notices = await self.apply_external_layer(Path(path), values)
        result = payload.setdefault("result", {})
        result["reloaded"] = reloaded
        result["errors"] = notices



__all__ = [
    "Loader",
    "LoaderComponent",
    "resolve_plugin_from_module",
]


def _config_layer(path: Path, values: dict[str, Any]) -> PluginTree:
    """Read one external plugin-tree layer; a missing file is an empty layer."""
    if not path.is_file():
        return PluginTree([])
    return PluginTree.from_yaml(path, values=values)


class LoaderComponent:
    """Mount the loader as a component: provides ``ctx.loader``."""

    name = "xbot.loader"

    def __init__(self, tree: PluginTree) -> None:
        self._tree = tree

    def apply(self, ctx: Context, config: Any = None) -> None:
        loader = Loader(ctx, tree=self._tree)
        ctx.set("loader", loader)
        ctx.on(Events.SOFT_RELOAD, loader.handle_soft_reload)
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
