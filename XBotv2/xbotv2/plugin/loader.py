"""Plugin discovery, dependency resolution, and registration."""

from __future__ import annotations

import importlib
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from xbotv2.core.context import ContextBuilder
from xbotv2.hooks.manager import HookManager
from xbotv2.api.hooks import HookStage
from xbotv2.api.plugins import PluginBase, PluginManifest
from xbotv2.persistence.store import CoreStateStore
from xbotv2.plugin.store import PluginStore
from xbotv2.tools.registry import ToolRegistry


@dataclass
class LoadedPluginRecord:
    """Runtime resources registered by one plugin."""

    plugin: Any
    hook_refs: list[tuple[HookStage, Any]] = field(default_factory=list)
    tool_names: list[str] = field(default_factory=list)
    fragment_stages: list[str] = field(default_factory=list)


@dataclass
class _PluginSetupContext:
    """Transactional adapter from the public plugin API to core services."""

    plugin_name: str
    hooks: HookManager
    tools: ToolRegistry
    context: ContextBuilder
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
        if len(added) != 1:
            raise RuntimeError("Plugin tool registration must add exactly one tool")
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


class PluginLoader:
    """Discover, load, and wire plugins into core components."""

    def __init__(
        self,
        *,
        plugin_dirs: list[Path],
        state_store: CoreStateStore,
        hook_manager: HookManager,
        tool_registry: ToolRegistry,
        context_builder: ContextBuilder,
        plugin_configs: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        self.plugin_dirs = plugin_dirs
        self.state_store = state_store
        self.hook_manager = hook_manager
        self.tool_registry = tool_registry
        self.context_builder = context_builder
        self.plugin_configs = plugin_configs or {}
        self.loaded_plugins: list[Any] = []
        self._records: dict[str, LoadedPluginRecord] = {}
        self._import_paths: list[str] = []

    def diagnostics(self) -> list[dict[str, Any]]:
        """Return serializable plugin health without exposing plugin objects."""
        result: list[dict[str, Any]] = []
        for plugin in self.loaded_plugins:
            details = plugin.diagnostics()
            result.append({
                "name": plugin.manifest.name,
                "version": plugin.manifest.version,
                "api_version": plugin.manifest.api_version,
                "status": details.pop("status", "ready"),
                "details": details,
            })
        return result

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
                with open(manifest_path) as f:
                    data = yaml.safe_load(f) or {}
                manifest = PluginManifest(**data)
                manifest.plugin_dir = candidate
                manifests.append((manifest, candidate))
        return manifests

    @staticmethod
    def resolve_order(
        manifests: list[tuple[PluginManifest, Path]],
    ) -> list[tuple[PluginManifest, Path]]:
        """Topologically sort manifests by depends_on."""
        return resolve_dependencies(manifests)

    async def load(self) -> list[Any]:
        """Discover, instantiate, initialize, and register plugins."""
        ordered = self.resolve_order(self.discover())
        for manifest, plugin_dir in ordered:
            plugin = None
            on_load_completed = False
            import_path_checkpoint = len(self._import_paths)
            try:
                self._ensure_importable(manifest, plugin_dir)
                plugin_store = PluginStore(self.state_store, manifest.name)
                plugin = instantiate_plugin(manifest, plugin_store)

                await plugin.on_load(self.plugin_configs.get(manifest.name, {}))
                on_load_completed = True
                self._records[manifest.name] = self._register(plugin)
            except Exception:
                if plugin is not None and on_load_completed:
                    try:
                        await plugin.on_unload()
                    finally:
                        if not self.loaded_plugins:
                            self._release_import_paths()
                        else:
                            self._release_import_paths_since(import_path_checkpoint)
                            await self.unload_all()
                        raise
                if not self.loaded_plugins:
                    self._release_import_paths()
                else:
                    self._release_import_paths_since(import_path_checkpoint)
                    await self.unload_all()
                raise
            self.loaded_plugins.append(plugin)
        return list(self.loaded_plugins)

    async def unload(self, plugin_name: str) -> bool:
        """Unload one plugin and remove its registered resources.

        Returns ``True`` when a loaded plugin was found and unloaded.
        """
        record = self._records.pop(plugin_name, None)
        if record is None:
            return False

        await record.plugin.on_unload()
        for stage, fn in reversed(record.hook_refs):
            self.hook_manager.unregister(stage, fn)
        for tool_name in reversed(record.tool_names):
            self.tool_registry.unregister(tool_name)
        self.tool_registry.unregister_plugin_tools(plugin_name)
        for stage in record.fragment_stages:
            self.context_builder.unregister_fragment(stage, plugin_name)
        self.loaded_plugins = [
            plugin
            for plugin in self.loaded_plugins
            if plugin.manifest.name != plugin_name
        ]
        if not self.loaded_plugins:
            self._release_import_paths()
        return True

    async def unload_all(self) -> list[str]:
        """Unload all loaded plugins in reverse load order."""
        unloaded: list[str] = []
        for plugin in reversed(list(self.loaded_plugins)):
            plugin_name = plugin.manifest.name
            if await self.unload(plugin_name):
                unloaded.append(plugin_name)
        return unloaded

    def _register(self, plugin: Any) -> LoadedPluginRecord:
        plugin_name = plugin.manifest.name
        setup = _PluginSetupContext(
            plugin_name=plugin_name,
            hooks=self.hook_manager,
            tools=self.tool_registry,
            context=self.context_builder,
        )
        try:
            plugin.setup(setup)
            return LoadedPluginRecord(
                plugin=plugin,
                hook_refs=setup.hook_refs,
                tool_names=setup.tool_names,
                fragment_stages=setup.fragment_stages,
            )
        except Exception:
            setup.rollback()
            raise

    def _ensure_importable(self, manifest: PluginManifest, plugin_dir: Path) -> None:
        plugin_pkg = f"builtin_plugins.{manifest.name}"
        try:
            importlib.import_module(plugin_pkg)
            return
        except ImportError:
            pass

        self._drop_stale_plugin_modules(manifest.name, plugin_dir)
        parent = str(plugin_dir.parent)
        if parent not in sys.path:
            sys.path.insert(0, parent)
            self._import_paths.append(parent)
        importlib.invalidate_caches()
        try:
            importlib.import_module(manifest.name)
        except ImportError:
            return

    @staticmethod
    def _drop_stale_plugin_modules(plugin_name: str, plugin_dir: Path) -> None:
        module = sys.modules.get(plugin_name)
        if module is None or _module_belongs_to_path(module, plugin_dir):
            return
        for name in list(sys.modules):
            if name == plugin_name or name.startswith(f"{plugin_name}."):
                sys.modules.pop(name, None)

    def _release_import_paths(self) -> None:
        self._release_import_paths_since(0)

    def _release_import_paths_since(self, index: int) -> None:
        for path in reversed(self._import_paths[index:]):
            try:
                sys.path.remove(path)
            except ValueError:
                pass
        del self._import_paths[index:]


def resolve_dependencies(
    manifests: list[tuple[PluginManifest, Path]],
) -> list[tuple[PluginManifest, Path]]:
    """Topological sort by dependency. Raises on cycles or missing deps."""
    name_to_item = {m.name: (m, p) for m, p in manifests}

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


def instantiate_plugin(manifest: PluginManifest, plugin_store: PluginStore) -> Any | None:
    """Instantiate a PluginBase subclass or manifest-driven default plugin."""
    class_name = "".join(part.title() for part in manifest.name.split("_")) + "Plugin"

    for module_name in [
        f"builtin_plugins.{manifest.name}.plugin",
        f"{manifest.name}.plugin",
        f"builtin_plugins.{manifest.name}",
        manifest.name,
    ]:
        try:
            module = importlib.import_module(module_name)
            if hasattr(module, class_name):
                cls = getattr(module, class_name)
                if issubclass(cls, PluginBase):
                    return cls(manifest, plugin_store)
        except (ImportError, AttributeError):
            continue

    return _DefaultPlugin(manifest, plugin_store)


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
    """Minimal plugin that uses manifest-driven hook/tool registration."""

    async def on_load(self, _config: dict[str, Any]) -> None:
        """No-op: _DefaultPlugin needs no initialization."""

    async def on_unload(self) -> None:
        """No-op: _DefaultPlugin has no resources outside registered core items."""
