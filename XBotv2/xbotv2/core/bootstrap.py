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
By default, plugins are discovered from the built-in plugin directory. Passing
``plugin_dirs=[]`` explicitly disables plugin discovery for pure-core runs.
"""

from __future__ import annotations

import importlib
import re
import uuid
from pathlib import Path
from typing import Any

from xbotv2.config.loader import load_agent_config, load_provider_config, load_user_context
from xbotv2.config.policy import (
    load_session_policy,
    merge_permission_config,
    merge_sandbox_config,
)
from xbotv2.core.context import ContextBuilder
from xbotv2.core.engine import Engine
from xbotv2.hooks.manager import HookManager
from xbotv2.hooks.types import HookContext, HookStage
from xbotv2.persistence.store import CoreStateStore
from xbotv2.plugin.loader import PluginLoader
from xbotv2.plugin.loader import _DefaultPlugin, resolve_dependencies as _resolve_dependencies
from xbotv2.core.workspace import SessionWorkspace
from xbotv2.tools.permissions import PermissionSystem
from xbotv2.tools.registry import ToolRegistry
from xbotv2.tools.sandbox import SandboxPolicy


# ------------------------------------------------------------------
# Core base tools (always registered, no plugin needed)
# Tools are defined in xbotv2.core.builtin_tools for clean separation.
# ------------------------------------------------------------------

from xbotv2.core.builtin_tools.filesystem import FILESYSTEM_TOOLS
from xbotv2.core.builtin_tools.interaction import INTERACTION_TOOLS
from xbotv2.core.builtin_tools.shell import SHELL_TOOLS
from xbotv2.tools.result_cache import make_tool_result_cache_hook

_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9._-]+$")

# (tool, sandbox_mode, execution_mode, lock_fields)
CORE_BASE_TOOLS = [
    (SHELL_TOOLS[0], "sandboxed", "sequential", ()),
    (FILESYSTEM_TOOLS[0], "sandboxed", "parallel", ("path",)),   # filesystem_read
    (FILESYSTEM_TOOLS[1], "sandboxed", "sequential", ("path",)),  # filesystem_write
    (FILESYSTEM_TOOLS[2], "sandboxed", "parallel", ("path",)),   # filesystem_list
    (INTERACTION_TOOLS[0], "host", "sequential", ()),  # send_message
    (INTERACTION_TOOLS[1], "host", "sequential", ()),  # ask_user
]


# ------------------------------------------------------------------
# Bootstrap
# ------------------------------------------------------------------

async def bootstrap(
    *,
    config_dir: Path | str = "data",
    personality_id: str = "default",
    provider_name: str = "default",
    session_id: str | None = None,
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
        plugin_dirs: Plugin directories to scan. ``None`` scans built-ins;
            an explicit empty list disables plugin discovery.
        plugin_configs: Per-plugin configuration dicts.
        llm_override: Use this LLM instead of loading from config (for testing).

    Returns:
        A fully-wired Engine ready to run turns.
    """
    config_dir = Path(config_dir)
    _validate_identifier("personality_id", personality_id)
    _validate_identifier("provider_name", provider_name)
    session_id = session_id or uuid.uuid4().hex
    _validate_identifier("session_id", session_id)
    _validate_identifier("thread_id", thread_id)
    _plugin_configs = plugin_configs or {}

    # 1. Load configuration
    agent_config = load_agent_config(config_dir, personality_id)
    provider_config = load_provider_config(config_dir, provider_name)
    load_user_context(config_dir)  # Validates config exists

    session_policy = load_session_policy(config_dir, session_id)
    agent_config.permissions = merge_permission_config(
        agent_config.permissions,
        session_policy.get("permissions"),
    )
    agent_config.sandbox = merge_sandbox_config(
        agent_config.sandbox,
        session_policy.get("sandbox"),
    )

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
    _register_configured_hooks(agent_config, hook_manager)

    # 4. Register core base tools (always available)
    for tool, sandbox_mode, execution_mode, lock_fields in CORE_BASE_TOOLS:
        tool_registry.register(
            tool,
            sandbox_mode=sandbox_mode,
            execution_mode=execution_mode,
            lock_fields=lock_fields,
            owner_plugin=None,  # Core-owned
        )

    # 5. Create SandboxPolicy + PermissionSystem
    session_root = config_dir / "sessions" / session_id
    workspace_root = session_root / "workspace"
    sandbox = SandboxPolicy(
        agent_config.sandbox,
        data_root=config_dir,
        workspace_root=workspace_root,
    )
    workspace = SessionWorkspace(
        workspace_root,
        session_id=session_id,
        thread_id=thread_id,
        base_root=session_root,
    )
    permissions = PermissionSystem(agent_config.permissions)

    # 6. Discover and load plugins. ``plugin_dirs=[]`` is a deliberate
    # no-plugin mode used by core freeze tests and pure-core embeddings.
    resolved_plugin_dirs = _resolve_plugin_dirs(plugin_dirs)

    if resolved_plugin_dirs:
        await _load_plugins(
            resolved_plugin_dirs,
            hook_manager,
            tool_registry,
            context_builder,
            state_store,
            _plugin_configs,
        )

    # Apply tool filter from personality config after plugins are loaded so
    # selectors can reference either core or plugin-provided tools.
    if agent_config.tools:
        tool_registry.restrict(agent_config.tools)

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
        workspace=workspace,
        config=agent_config,
        data_dir=str(config_dir),
    )

    return engine


# ------------------------------------------------------------------
# Internal helpers
# ------------------------------------------------------------------

def _validate_identifier(field: str, value: str) -> None:
    if not value or value in {".", ".."} or not _IDENTIFIER_RE.fullmatch(value):
        raise ValueError(
            f"{field} must be a non-empty identifier using letters, numbers, '.', '_', or '-'"
        )


def _resolve_plugin_dirs(
    plugin_dirs: list[Path | str] | None,
    *,
    builtin_plugins_dir: Path | None = None,
) -> list[Path]:
    """Resolve plugin scan directories.

    ``None`` means the normal runtime default: scan built-in plugins. An
    explicit empty list means no plugin discovery at all.
    """
    if plugin_dirs is not None:
        return [Path(d) for d in plugin_dirs]

    builtin_dir = (
        builtin_plugins_dir
        if builtin_plugins_dir is not None
        else Path(__file__).parent.parent.parent / "builtin_plugins"
    )
    return [builtin_dir] if builtin_dir.exists() else []


async def _load_plugins(
    plugin_dirs: list[Path],
    hook_manager: HookManager,
    tool_registry: ToolRegistry,
    context_builder: ContextBuilder,
    state_store: CoreStateStore,
    plugin_configs: dict[str, dict[str, Any]],
) -> None:
    """Discover, load, and wire plugins."""
    loader = PluginLoader(
        plugin_dirs=plugin_dirs,
        state_store=state_store,
        hook_manager=hook_manager,
        tool_registry=tool_registry,
        context_builder=context_builder,
        plugin_configs=plugin_configs,
    )
    await loader.load()


def _register_configured_hooks(agent_config: Any, hook_manager: HookManager) -> None:
    """Register hooks declared directly in the personality config."""
    for decl in getattr(agent_config, "hooks", []) or []:
        hook_manager.register(HookStage(decl.stage), _resolve_hook_target(decl.target))


def _resolve_hook_target(target: str) -> Any:
    """Resolve ``module:function`` hook targets from personality config."""
    if ":" not in target:
        raise ValueError(f"Invalid hook target {target!r}; expected 'module:function'")
    module_path, attr_name = target.split(":", 1)
    if not module_path or not attr_name:
        raise ValueError(f"Invalid hook target {target!r}; expected 'module:function'")
    module = importlib.import_module(module_path)
    try:
        return getattr(module, attr_name)
    except AttributeError as exc:
        raise ImportError(f"Hook target {target!r} does not exist") from exc
