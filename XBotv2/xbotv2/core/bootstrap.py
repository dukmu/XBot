"""Bootstrap the complete XBotv2 runtime from configuration.

Sequence:
1. Load configuration (personality.yaml, provider.yaml, user.yaml)
2. Create CoreStateStore
3. Create empty HookManager, ToolRegistry, ContextBuilder
4. Register core base tools
5. Create SandboxPolicy + PermissionSystem
6. Discover and load plugins
7. Register plugin hooks, tools, prompt fragments
8. Create LLM client
9. Run ON_SESSION_INIT hooks
10. Return fully-wired Engine

Architecture constraint: bootstrap NEVER hardcodes plugin references.
Plugins are discovered from plugin directories listed in config.
"""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any

from xbotv2.config.loader import load_agent_config, load_provider_config, load_user_context
from xbotv2.core.context import ContextBuilder
from xbotv2.core.engine import Engine
from xbotv2.hooks.manager import HookManager
from xbotv2.hooks.types import HookContext, HookStage
from xbotv2.persistence.store import CoreStateStore
from xbotv2.plugin.manifest import PluginManifest
from xbotv2.tools.permissions import PermissionSystem
from xbotv2.tools.registry import ToolRegistry
from xbotv2.tools.sandbox import SandboxPolicy


# ------------------------------------------------------------------
# Core base tools (always registered, no plugin needed)
# Tools are defined in xbotv2.core.builtin_tools for clean separation.
# ------------------------------------------------------------------

from xbotv2.core.builtin_tools.filesystem import FILESYSTEM_TOOLS
from xbotv2.core.builtin_tools.shell import SHELL_TOOLS
from xbotv2.tools.result_cache import make_tool_result_cache_hook

# (tool, sandbox_mode, execution_mode, lock_fields)
CORE_BASE_TOOLS = [
    (SHELL_TOOLS[0], "sandboxed", "sequential", ()),
    (FILESYSTEM_TOOLS[0], "sandboxed", "parallel", ("path",)),   # filesystem_read
    (FILESYSTEM_TOOLS[1], "sandboxed", "sequential", ("path",)),  # filesystem_write
    (FILESYSTEM_TOOLS[2], "sandboxed", "parallel", ("path",)),   # filesystem_list
]


# ------------------------------------------------------------------
# Bootstrap
# ------------------------------------------------------------------

async def bootstrap(
    *,
    config_dir: Path | str = "data",
    personality_id: str = "default",
    provider_name: str = "default",
    session_id: str = "default",
    thread_id: str = "agent",
    plugin_dirs: list[Path | str] | None = None,
    plugin_configs: dict[str, dict[str, Any]] | None = None,
    llm_override: Any | None = None,
) -> Engine:
    """Bootstrap the complete XBotv2 runtime.

    Args:
        config_dir: Root data directory with config/, personalities/, sessions/.
        personality_id: Personality to load.
        provider_name: Provider config name.
        session_id: Session identifier.
        thread_id: LangGraph thread ID.
        plugin_dirs: Additional plugin directories to scan.
        plugin_configs: Per-plugin configuration dicts.
        llm_override: Use this LLM instead of loading from config (for testing).

    Returns:
        A fully-wired Engine ready to run turns.
    """
    config_dir = Path(config_dir)
    _plugin_configs = plugin_configs or {}

    # 1. Load configuration
    agent_config = load_agent_config(config_dir, personality_id)
    provider_config = load_provider_config(config_dir, provider_name)
    load_user_context(config_dir)  # Validates config exists

    # Merge plugin configs from personality
    if agent_config.plugins:
        _plugin_configs = {**_plugin_configs, **agent_config.plugins}

    # Ensure session state directory
    state_root = config_dir / "sessions" / session_id / "state"

    # 2. Create CoreStateStore
    state_store = CoreStateStore.create(
        state_root,
        session_id=session_id,
        thread_id=thread_id,
        personality_id=personality_id,
    )

    # 3. Create empty core components
    hook_manager = HookManager()
    tool_registry = ToolRegistry()
    context_builder = ContextBuilder()
    hook_manager.register(
        HookStage.AFTER_TOOLS,
        make_tool_result_cache_hook(state_store),
    )

    # 4. Register core base tools (always available)
    for tool, sandbox_mode, execution_mode, lock_fields in CORE_BASE_TOOLS:
        tool_registry.register(
            tool,
            sandbox_mode=sandbox_mode,
            execution_mode=execution_mode,
            lock_fields=lock_fields,
            owner_plugin=None,  # Core-owned
        )

    # Apply tool filter from personality config (limits what the agent sees)
    if agent_config.tools:
        tool_registry.restrict(agent_config.tools)

    # 5. Create SandboxPolicy + PermissionSystem
    sandbox = SandboxPolicy(
        agent_config.sandbox,
        data_root=config_dir,
        workspace_root=config_dir / "sessions" / session_id / "workspace",
    )
    permissions = PermissionSystem(agent_config.permissions)

    # 6. Discover and load plugins
    resolved_plugin_dirs: list[Path] = []
    if plugin_dirs:
        resolved_plugin_dirs = [Path(d) for d in plugin_dirs]
    # Also scan builtin_plugins relative to the xbotv2 package
    builtin_plugins_dir = Path(__file__).parent.parent.parent / "builtin_plugins"
    if builtin_plugins_dir.exists() and builtin_plugins_dir not in resolved_plugin_dirs:
        resolved_plugin_dirs.append(builtin_plugins_dir)

    if resolved_plugin_dirs:
        await _load_plugins(
            resolved_plugin_dirs,
            hook_manager,
            tool_registry,
            context_builder,
            state_store,
            agent_config.plugins if agent_config.plugins else {},
        )

    # 7. Create LLM client
    if llm_override is not None:
        llm = llm_override
    else:
        from xbotv2.llm.client import create_llm
        llm = create_llm(provider_config)

    # 8. Run ON_SESSION_INIT hooks
    from xbotv2.core.state import SessionInfo
    init_ctx = HookContext(
        stage=HookStage.ON_SESSION_INIT,
        state={},
        config=agent_config,
        tools=tool_registry,
        plugin_store=None,
        session=SessionInfo(
            session_id=session_id,
            thread_id=thread_id,
            personality_id=personality_id,
        ),
        emit=lambda e: state_store.append_event("hook_event", e),
    )
    await hook_manager.run(HookStage.ON_SESSION_INIT, init_ctx, short_circuit=False)

    # 9. Build engine
    engine = Engine(
        llm=llm,
        tool_registry=tool_registry,
        hook_manager=hook_manager,
        state_store=state_store,
        context_builder=context_builder,
        sandbox_policy=sandbox,
        permission_system=permissions,
        config=agent_config,
    )

    return engine


# ------------------------------------------------------------------
# Internal helpers
# ------------------------------------------------------------------

async def _load_plugins(
    plugin_dirs: list[Path],
    hook_manager: HookManager,
    tool_registry: ToolRegistry,
    context_builder: ContextBuilder,
    state_store: CoreStateStore,
    plugin_configs: dict[str, dict[str, Any]],
) -> None:
    """Discover, load, and wire plugins."""
    import yaml

    from xbotv2.plugin.store import PluginStore

    manifests: list[tuple[PluginManifest, Path]] = []

    # Discover
    for plugin_dir in plugin_dirs:
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

    if not manifests:
        return

    # Resolve dependency order (topological sort)
    ordered = _resolve_dependencies(manifests)

    # Load each plugin
    for manifest, plugin_dir in ordered:
        # Import the plugin module
        plugin_pkg = f"builtin_plugins.{manifest.name}"
        try:
            importlib.import_module(plugin_pkg)
        except ImportError:
            # Try direct path import
            import sys
            sys.path.insert(0, str(plugin_dir.parent))
            try:
                importlib.import_module(manifest.name)
            except ImportError:
                continue
            finally:
                sys.path.pop(0)

        # Create plugin store
        plugin_store = PluginStore(state_store, manifest.name)

        # Load plugin class
        plugin = _instantiate_plugin(manifest, plugin_store, plugin_dir)

        if plugin is not None:
            # Initialize
            config = plugin_configs.get(manifest.name, {})
            await plugin.on_load(config)

            # Register hooks, tools, prompt fragments
            plugin.register_hooks(hook_manager)
            plugin.register_tools(tool_registry)

            for stage, text in plugin.get_prompt_fragments().items():
                context_builder.register_fragment(stage, manifest.name, text)


def _resolve_dependencies(
    manifests: list[tuple[PluginManifest, Path]],
) -> list[tuple[PluginManifest, Path]]:
    """Topological sort by dependency. Raises on cycles or missing deps."""
    name_to_item = {m.name: (m, p) for m, p in manifests}

    # Check missing dependencies
    for manifest, _ in manifests:
        for dep in manifest.depends_on:
            if dep not in name_to_item:
                raise ValueError(
                    f"Plugin '{manifest.name}' depends on '{dep}', "
                    f"which is not available"
                )

    # Kahn's algorithm
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


def _instantiate_plugin(
    manifest: Any, plugin_store: Any, _plugin_dir: Path
) -> Any | None:
    """Try to instantiate a plugin class.

    Args:
        manifest: PluginManifest.
        plugin_store: PluginStore for the plugin.
        _plugin_dir: Plugin directory on disk (reserved for future use).
    """
    from xbotv2.plugin.base import PluginBase

    # Convention: class name is <Name>Plugin
    class_name = "".join(part.title() for part in manifest.name.split("_")) + "Plugin"

    # Try the plugin's main module
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

    # Fallback: a default PluginBase subclass that uses manifest-driven registration
    return _DefaultPlugin(manifest, plugin_store)


class _DefaultPlugin:
    """Minimal plugin that uses manifest-driven hook/tool registration."""

    def __init__(self, manifest, store):
        self.manifest = manifest
        self.store = store

    async def on_load(self, _config: dict[str, Any]) -> None:
        """No-op: _DefaultPlugin needs no initialization."""

    def register_hooks(self, manager):
        from xbotv2.hooks.types import HookStage
        for decl in self.manifest.hooks:
            handler = self._resolve(decl.handler)
            if handler:
                manager.register(HookStage(decl.stage), handler)

    def register_tools(self, registry):
        for decl in self.manifest.tools:
            tool = self._resolve(decl.handler)
            if tool:
                registry.register(
                    tool,
                    sandbox_mode=decl.sandbox_mode,
                    execution_mode=decl.execution_mode,
                    lock_fields=tuple(decl.lock_fields),
                    owner_plugin=self.manifest.name,
                )

    def get_prompt_fragments(self):
        fragments = {}
        for decl in self.manifest.prompt_fragments:
            if decl.handler:
                handler = self._resolve(decl.handler)
                if handler:
                    fragments[decl.stage] = handler() if callable(handler) else str(handler)
            elif decl.file:
                try:
                    base_dir = self.manifest.plugin_dir or Path.cwd()
                    fragments[decl.stage] = (base_dir / decl.file).read_text()
                except Exception:
                    fragments[decl.stage] = ""
        return fragments

    @staticmethod
    def _resolve(dotted_path: str):
        try:
            module_path, _, attr = dotted_path.partition(":")
            if not attr:
                return None
            module = importlib.import_module(module_path)
            return getattr(module, attr)
        except Exception:
            return None
