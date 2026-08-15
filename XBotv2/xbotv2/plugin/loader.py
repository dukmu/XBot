"""Plugin discovery, dependency resolution, and XCore-based loading.

The loader's job is unchanged -- discover ``plugin.yaml`` manifests, order by
``depends_on``, validate configs, instantiate :class:`PluginBase` subclasses --
but mounting now goes through the XCore plugin registry (``ctx.plugin``):
lifecycle, hook/listener ownership, and cleanup are owned by XCore fibers
(design: ``XCore/docs/05-migration-plan.md``).  The manual rollback tables
(hook_refs / tool_names / ...) are gone; unload = dispose the fiber.
"""

from __future__ import annotations

import importlib
import inspect
import logging
import sys
from collections import Counter
from pathlib import Path
from typing import Any

import yaml

from xcore import Context

from xbotv2.api.plugins import (
    PluginBase,
    PluginManifest,
)
from xbotv2.plugin.bridge import PluginAdapter

logger = logging.getLogger("xbotv2.plugin_loader")


class PluginLoader:
    """Discover, load, and wire plugins into an XCore context."""

    def __init__(
        self,
        *,
        ctx: Context,
        plugin_dirs: list[Path],
        plugin_configs: dict[str, dict[str, Any]] | None = None,
        disabled_plugins: set[str] | None = None,
        workspace_root: Path | str | None = None,
    ) -> None:
        self.ctx = ctx
        self.plugin_dirs = plugin_dirs
        self.plugin_configs = plugin_configs or {}
        self.disabled_plugins = disabled_plugins or set()
        self.workspace_root = Path(workspace_root or Path.cwd())
        self._plugins: dict[str, PluginBase] = {}
        self._handles: dict[str, Any] = {}
        self._import_paths: list[str] = []

    # -- inspection ---------------------------------------------------------

    @property
    def loaded_plugins(self) -> list[Any]:
        return list(self._plugins.values())

    @property
    def commands(self) -> tuple[Any, ...]:
        """All registered commands (including dynamic ones)."""
        return self.ctx.commands.all()

    def get_command(self, name: str) -> Any | None:
        return self.ctx.commands.get(name)

    def diagnostics(self) -> list[dict[str, Any]]:
        """Return serializable plugin health without exposing plugin objects."""
        result: list[dict[str, Any]] = []
        for plugin in self.loaded_plugins:
            details = dict(plugin.diagnostics())
            result.append({
                "name": plugin.manifest.name,
                "version": plugin.manifest.version,
                "api_version": plugin.manifest.api_version,
                "status": details.pop("status", "ready"),
                "details": details,
            })
        return result

    async def status_slots(self) -> dict[str, str]:
        """Collect validated status slots without exposing plugin objects."""
        slots: dict[str, str] = {}
        for plugin in self.loaded_plugins:
            try:
                values = await plugin.status_slots()
            except Exception:
                logger.exception(
                    "Plugin %s status slots failed", plugin.manifest.name
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

    # -- discovery ----------------------------------------------------------

    def discover(self) -> list[tuple[PluginManifest, Path]]:
        """Scan plugin directories for plugin.yaml manifests."""
        manifests: list[tuple[PluginManifest, Path]] = []
        for plugin_dir in self.plugin_dirs:
            if not plugin_dir.exists():
                continue
            for candidate in sorted(plugin_dir.iterdir()):
                if not candidate.is_dir():
                    continue
                manifest_path = candidate / "plugin.yaml"
                if not manifest_path.exists():
                    continue
                with open(manifest_path, encoding="utf-8") as f:
                    data = yaml.safe_load(f) or {}
                manifest = PluginManifest(**data)
                if manifest.name in self.disabled_plugins:
                    continue
                manifest.plugin_dir = candidate
                manifests.append((manifest, candidate))
        return manifests

    # -- loading ------------------------------------------------------------

    async def load(self) -> list[Any]:
        """Discover, instantiate, and mount plugins in dependency order.

        The XCore context is started on first load so mounted plugins load
        immediately (the loader owns plugin lifecycle; the engine owns
        teardown through :meth:`unload_all`).
        """
        if not self.ctx.is_active:
            await self.ctx.start()
        ordered = resolve_dependencies(self.discover())
        for manifest, plugin_dir in ordered:
            plugin = None
            mounted = False
            try:
                plugin_config = dict(self.plugin_configs.get(manifest.name, {}))
                manifest.validate_config(plugin_config)
                self._ensure_importable(manifest, plugin_dir)
                plugin = instantiate_plugin(manifest)
                await plugin.on_load(plugin_config)
                adapter = PluginAdapter(plugin)
                handle = self.ctx.plugin(adapter, plugin_config)
                mounted = True
                await handle
                self._plugins[manifest.name] = plugin
                self._handles[manifest.name] = handle
            except BaseException as load_error:
                cleanup_errors: list[BaseException] = []
                # ``on_unload`` is a fiber disposer once mounted; only call it
                # directly when ``on_load`` failed before the adapter mounted.
                if plugin is not None and not mounted:
                    try:
                        await plugin.on_unload()
                    except BaseException as cleanup_error:
                        cleanup_errors.append(cleanup_error)
                try:
                    await self.unload_all()
                except BaseException as cleanup_error:
                    cleanup_errors.append(cleanup_error)
                self._release_import_paths()
                for cleanup_error in cleanup_errors:
                    load_error.add_note(f"Plugin cleanup also failed: {cleanup_error!r}")
                raise
        return list(self.loaded_plugins)

    async def unload(self, plugin_name: str) -> bool:
        """Unload one plugin (disposes its XCore fiber).

        ``on_unload`` runs once as the fiber's disposer (registered by the
        plugin adapter); its failures are logged, never raised (XCore
        lifecycle semantics).
        """
        handle = self._handles.pop(plugin_name, None)
        if handle is None:
            return False
        self._plugins.pop(plugin_name, None)
        await handle.dispose()
        if not self._handles:
            self._release_import_paths()
        return True

    async def reload(self, plugin_name: str) -> bool:
        """Reload one active plugin from its existing manifest."""
        record = self._plugins.get(plugin_name)
        if record is None:
            return False
        manifest = record.manifest
        plugin_dir = manifest.plugin_dir
        was_unloaded = await self.unload(plugin_name)
        if not was_unloaded:
            return False
        self._ensure_importable(manifest, plugin_dir or Path.cwd())
        plugin = instantiate_plugin(manifest)
        try:
            config = dict(self.plugin_configs.get(manifest.name, {}))
            manifest.validate_config(config)
            await plugin.on_load(config)
            adapter = PluginAdapter(plugin)
            handle = self.ctx.plugin(adapter, config)
            await handle
            self._plugins[manifest.name] = plugin
            self._handles[manifest.name] = handle
        except BaseException as reload_error:
            try:
                await plugin.on_unload()
            except BaseException as cleanup_error:
                reload_error.add_note(
                    f"Plugin cleanup also failed: {cleanup_error!r}"
                )
            raise
        return True

    async def unload_all(self) -> list[str]:
        """Unload all loaded plugins in reverse load order."""
        unloaded: list[str] = []
        errors: list[BaseException] = []
        for plugin_name in reversed(list(self._handles)):
            try:
                was_unloaded = await self.unload(plugin_name)
            except BaseException as exc:
                was_unloaded = plugin_name not in self._handles
                errors.append(exc)
            if was_unloaded:
                unloaded.append(plugin_name)
        if errors:
            raise BaseExceptionGroup("One or more plugins failed during unload", errors)
        return unloaded

    # -- import path management ---------------------------------------------

    def _ensure_importable(self, manifest: PluginManifest, plugin_dir: Path) -> None:
        if _is_builtin_plugin_dir(plugin_dir):
            return

        self._drop_stale_plugin_modules(manifest.name, plugin_dir)
        parent = str(plugin_dir.parent)
        if parent not in sys.path:
            sys.path.insert(0, parent)
            self._import_paths.append(parent)
        importlib.invalidate_caches()

    @staticmethod
    def _drop_stale_plugin_modules(plugin_name: str, plugin_dir: Path) -> None:
        module = sys.modules.get(plugin_name)
        if module is None or _module_belongs_to_path(module, plugin_dir):
            return
        for name in list(sys.modules):
            if name == plugin_name or name.startswith(f"{plugin_name}."):
                sys.modules.pop(name, None)

    def _release_import_paths(self) -> None:
        for path in reversed(self._import_paths):
            try:
                sys.path.remove(path)
            except ValueError:
                pass
        self._import_paths.clear()


def resolve_dependencies(
    manifests: list[tuple[PluginManifest, Path]],
) -> list[tuple[PluginManifest, Path]]:
    """Topological sort by dependency. Raises on cycles or missing deps."""
    name_to_item = {m.name: (m, p) for m, p in manifests}
    if len(name_to_item) != len(manifests):
        counts = Counter(manifest.name for manifest, _ in manifests)
        duplicates = sorted(name for name, count in counts.items() if count > 1)
        raise ValueError(f"Duplicate plugin manifests: {', '.join(duplicates)}")

    for manifest, _ in manifests:
        for dep in manifest.depends_on:
            if dep not in name_to_item:
                raise ValueError(
                    f"Plugin '{manifest.name}' depends on '{dep}', "
                    f"which is not available"
                )

    in_degree: dict[str, int] = {m.name: len(m.depends_on) for m, _ in manifests}
    adj: dict[str, list[str]] = {m.name: [] for m, _ in manifests}
    for manifest, _ in manifests:
        for dep in manifest.depends_on:
            adj[dep].append(manifest.name)

    queue = [name for name, deg in in_degree.items() if deg == 0]
    result: list[tuple[PluginManifest, Path]] = []

    while queue:
        name = queue.pop(0)
        result.append(name_to_item[name])
        for neighbor in adj.get(name, []):
            in_degree[neighbor] -= 1
            if in_degree[neighbor] == 0:
                queue.append(neighbor)

    if len(result) != len(manifests):
        remaining = [m.name for m, _ in manifests if m.name not in {r[0].name for r in result}]
        raise ValueError(f"Circular dependency detected among plugins: {remaining}")

    return result


def instantiate_plugin(manifest: PluginManifest) -> Any:
    """Instantiate a PluginBase subclass or manifest-driven default plugin."""
    class_name = "".join(part.title() for part in manifest.name.split("_")) + "Plugin"
    package = (
        f"builtin_plugins.{manifest.name}"
        if manifest.plugin_dir is not None
        and _is_builtin_plugin_dir(manifest.plugin_dir)
        else manifest.name
    )
    for module_name in (f"{package}.plugin", package):
        try:
            module = importlib.import_module(module_name)
        except ModuleNotFoundError as exc:
            if exc.name and (
                exc.name == module_name
                or module_name.startswith(f"{exc.name}.")
            ):
                continue
            raise
        if (
            manifest.plugin_dir is not None
            and not _module_belongs_to_path(module, manifest.plugin_dir)
        ):
            raise ImportError(
                f"Plugin module {module_name!r} was not loaded from "
                f"{manifest.plugin_dir}"
            )
        cls = getattr(module, class_name, None)
        if cls is None:
            continue
        if not isinstance(cls, type) or not issubclass(cls, PluginBase):
            raise TypeError(
                f"Plugin class {module_name}:{class_name} must inherit PluginBase"
            )
        return cls(manifest)

    return _DefaultPlugin(manifest)


def _is_builtin_plugin_dir(plugin_dir: Path) -> bool:
    builtin_root = Path(__file__).parent.parent.parent / "builtin_plugins"
    try:
        plugin_dir.resolve().relative_to(builtin_root.resolve())
        return True
    except ValueError:
        return False


def _module_belongs_to_path(module: Any, plugin_dir: Path) -> bool:
    plugin_dir = plugin_dir.resolve()
    module_file = getattr(module, "__file__", None)
    if module_file:
        try:
            Path(module_file).resolve().relative_to(plugin_dir)
            return True
        except ValueError:
            return False
    module_paths = getattr(module, "__path__", None)
    if module_paths:
        for raw_path in module_paths:
            try:
                Path(raw_path).resolve().relative_to(plugin_dir)
                return True
            except ValueError:
                continue
    return False


class _DefaultPlugin(PluginBase):
    """Minimal plugin that mounts manifest-declared resources in ``apply``."""

    async def on_load(self, _config: dict[str, Any]) -> None:
        """No-op: _DefaultPlugin needs no initialization."""

    async def on_unload(self) -> None:
        """No-op: registrations are fiber effects."""

    def apply(self, ctx: Any) -> None:
        """Register manifest-declared hooks and tools (none today)."""
        from xbotv2.api.hooks import HookStage
        from xbotv2.api.plugins import ToolRegistrationOptions

        for declaration in self.manifest.hooks:
            ctx.on(
                HookStage(declaration.stage).value,
                self._resolve(declaration.handler),
            )
        for declaration in self.manifest.tools:
            ctx.tools.register(
                self._resolve(declaration.handler),
                options=ToolRegistrationOptions(
                    sandbox_mode=declaration.sandbox_mode,
                ),
            )

    @staticmethod
    def _resolve(dotted_path: str) -> Any:
        module_path, separator, attribute = dotted_path.partition(":")
        if not separator or not attribute:
            raise ValueError(
                f"Invalid handler path (expected module:attribute): {dotted_path!r}"
            )
        return getattr(importlib.import_module(module_path), attribute)
