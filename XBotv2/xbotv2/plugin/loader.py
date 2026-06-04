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
from xbotv2.hooks.types import HookStage
from xbotv2.persistence.store import CoreStateStore
from xbotv2.plugin.base import PluginBase
from xbotv2.plugin.manifest import PluginManifest
from xbotv2.plugin.store import PluginStore
from xbotv2.tools.registry import ToolRegistry


@dataclass
class LoadedPluginRecord:
    """Runtime resources registered by one plugin."""

    plugin: Any
    hook_refs: list[tuple[HookStage, Any]] = field(default_factory=list)
    tool_names: list[str] = field(default_factory=list)
    fragment_stages: list[str] = field(default_factory=list)


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
                        raise
                if not self.loaded_plugins:
                    self._release_import_paths()
                else:
                    self._release_import_paths_since(import_path_checkpoint)
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
        before_hooks = {
            stage: list(self.hook_manager._hooks.get(stage, []))
            for stage in HookStage
        }
        before_tools = set(self.tool_registry.registered_names())
        before_fragments = {
            stage: set(self.context_builder._fragments.get(stage, {}))
            for stage in self.context_builder.FRAGMENT_STAGES
        }

        try:
            plugin.register_hooks(self.hook_manager)
            plugin.register_tools(self.tool_registry)
            for stage, text in plugin.get_prompt_fragments().items():
                self.context_builder.register_fragment(stage, plugin_name, text)

            hook_refs: list[tuple[HookStage, Any]] = []
            for stage in HookStage:
                previous = before_hooks.get(stage, [])
                current = list(self.hook_manager._hooks.get(stage, []))
                for fn in current[len(previous):]:
                    hook_refs.append((stage, fn))

            tool_names = [
                name
                for name in self.tool_registry.registered_names()
                if name not in before_tools
            ]
            fragment_stages = [
                stage
                for stage in self.context_builder.FRAGMENT_STAGES
                if plugin_name in self.context_builder._fragments.get(stage, {})
                and plugin_name not in before_fragments.get(stage, set())
            ]
            return LoadedPluginRecord(
                plugin=plugin,
                hook_refs=hook_refs,
                tool_names=tool_names,
                fragment_stages=fragment_stages,
            )
        except Exception:
            self._rollback_registration(plugin_name, before_hooks, before_tools, before_fragments)
            raise

    def _rollback_registration(
        self,
        plugin_name: str,
        before_hooks: dict[HookStage, list[Any]],
        before_tools: set[str],
        before_fragments: dict[str, set[str]],
    ) -> None:
        for stage in HookStage:
            self.hook_manager._hooks[stage] = before_hooks.get(stage, [])
        for tool_name in list(self.tool_registry.registered_names()):
            if tool_name not in before_tools:
                self.tool_registry.unregister(tool_name)
        self.tool_registry.unregister_plugin_tools(plugin_name)
        for stage in self.context_builder.FRAGMENT_STAGES:
            if plugin_name in self.context_builder._fragments.get(stage, {}):
                if plugin_name not in before_fragments.get(stage, set()):
                    self.context_builder.unregister_fragment(stage, plugin_name)

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


class _DefaultPlugin:
    """Minimal plugin that uses manifest-driven hook/tool registration."""

    def __init__(self, manifest: PluginManifest, store: PluginStore | None) -> None:
        self.manifest = manifest
        self.store = store

    async def on_load(self, _config: dict[str, Any]) -> None:
        """No-op: _DefaultPlugin needs no initialization."""

    async def on_unload(self) -> None:
        """No-op: _DefaultPlugin has no resources outside registered core items."""

    def register_hooks(self, manager: HookManager) -> None:
        for decl in self.manifest.hooks:
            manager.register(HookStage(decl.stage), self._resolve(decl.handler))

    def register_tools(self, registry: ToolRegistry) -> None:
        for decl in self.manifest.tools:
            registry.register(
                self._resolve(decl.handler),
                sandbox_mode=decl.sandbox_mode,
                execution_mode=decl.execution_mode,
                lock_fields=tuple(decl.lock_fields),
                owner_plugin=self.manifest.name,
            )

    def get_prompt_fragments(self) -> dict[str, str]:
        fragments: dict[str, str] = {}
        for decl in self.manifest.prompt_fragments:
            if decl.handler:
                handler = self._resolve(decl.handler)
                fragments[decl.stage] = handler() if callable(handler) else str(handler)
            elif decl.file:
                base_dir = self.manifest.plugin_dir or Path.cwd()
                file_path = base_dir / decl.file
                if not file_path.exists():
                    raise FileNotFoundError(
                        f"Plugin '{self.manifest.name}' prompt fragment file not found: {file_path}"
                    )
                fragments[decl.stage] = file_path.read_text()
        return fragments

    @staticmethod
    def _resolve(dotted_path: str) -> Any:
        module_path, _, attr = dotted_path.partition(":")
        if not attr:
            raise ValueError(f"Invalid handler path (missing ':attr'): {dotted_path!r}")
        try:
            module = importlib.import_module(module_path)
        except ImportError as exc:
            raise ImportError(f"Could not import plugin handler module {module_path!r}") from exc
        try:
            return getattr(module, attr)
        except AttributeError as exc:
            raise AttributeError(
                f"Plugin handler {attr!r} not found in module {module_path!r}"
            ) from exc
