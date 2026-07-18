"""Bootstrap the complete XBotv2 runtime from configuration.

Sequence:
1. Load global configuration and startup-only workspace overlays
2. Create CoreStateStore
3. Create empty HookManager, ToolRegistry, ContextBuilder
4. Register core base tools
5. Create SandboxPolicy + PermissionSystem
6. Discover and load plugins
7. Register plugin hooks, tools, prompt fragments, and Agent definitions
8. Create LLM client
9. Run ON_SESSION_INIT hooks
10. Return fully-wired Engine

Architecture constraint: bootstrap NEVER hardcodes plugin references.
By default, plugins are discovered from the built-in plugin directory. Passing
``plugin_dirs=[]`` explicitly disables plugin discovery for pure-core runs.
"""

from __future__ import annotations

import importlib
import importlib.util
import re
import secrets
import shutil
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any

from xbotv2.config.loader import load_provider_config, load_system_config, load_user_context
from xbotv2.config.policy import (
    load_session_policy,
    merge_permission_config,
    merge_sandbox_config,
)
from xbotv2.api.agents import AgentDefinition
from xbotv2.api.paths import RuntimePaths
from xbotv2.core.context import ContextBuilder
from xbotv2.core.agents import (
    AgentRegistry,
    apply_agent_definition,
    apply_agent_provider,
    apply_agent_tools,
)
from xbotv2.core.subagents import SubagentManager
from xbotv2.core.background_tasks import BackgroundTaskManager
from xbotv2.core.engine import Engine
from xbotv2.hooks.manager import HookManager
from xbotv2.api.hooks import HookContext, HookStage
from xbotv2.persistence.store import CoreStateStore
from xbotv2.plugin.loader import PluginLoader
from xbotv2.tools.permissions import PermissionIntersection, PermissionSystem
from xbotv2.tools.registry import ToolRegistry
from xbotv2.tools.sandbox import SandboxPolicy


# ------------------------------------------------------------------
# Core base tools (always registered, no plugin needed)
# Tools are defined in xbotv2.core.builtin_tools for clean separation.
# ------------------------------------------------------------------

from xbotv2.core.builtin_tools.filesystem import FILESYSTEM_TOOLS
from xbotv2.core.builtin_tools.interaction import INTERACTION_TOOLS
from xbotv2.tools.result_cache import make_tool_result_cache_hook

_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_SUBAGENT_BLOCKED_PLUGINS = {"agents"}

# (tool, sandbox_mode)
CORE_BASE_TOOLS = [
    (FILESYSTEM_TOOLS[0], "sandboxed"),  # filesystem_read
    (FILESYSTEM_TOOLS[1], "sandboxed"),  # filesystem_write
    (FILESYSTEM_TOOLS[2], "sandboxed"),  # filesystem_list
    (FILESYSTEM_TOOLS[3], "sandboxed"),  # search_text
    (FILESYSTEM_TOOLS[4], "sandboxed"),  # find_files
    (INTERACTION_TOOLS[0], "host"),  # send_message
    (INTERACTION_TOOLS[1], "host"),  # ask_user
]


# ------------------------------------------------------------------
# Bootstrap
# ------------------------------------------------------------------

async def bootstrap(
    *,
    paths: RuntimePaths,
    provider_name: str = "default",
    session_id: str | None = None,
    thread_id: str = "agent",
    workspace_root: Path | str | None = None,
    plugin_dirs: list[Path | str] | None = None,
    plugin_configs: dict[str, dict[str, Any]] | None = None,
    llm_override: Any | None = None,
    selected_agent: str | None = None,
    agent_definition: AgentDefinition | None = None,
    parent_permission_system: Any | None = None,
    parent_thread_id: str = "",
    subagent_depth: int = 0,
) -> Engine:
    """Bootstrap the complete XBotv2 runtime.

    Args:
        paths: Canonical runtime filesystem layout.
        provider_name: Provider config name.
        session_id: Session identifier.
        thread_id: session thread identifier.
        workspace_root: External workspace root. Defaults to current directory.
        plugin_dirs: Plugin directories to scan. ``None`` scans built-ins;
            an explicit empty list disables plugin discovery.
        plugin_configs: Per-plugin configuration dicts.
        llm_override: Use this LLM instead of loading from config (for testing).

    Returns:
        A fully-wired Engine ready to run turns.
    """
    _validate_identifier("provider_name", provider_name)
    session_id = session_id or _new_session_id()
    _validate_identifier("session_id", session_id)
    _validate_identifier("thread_id", thread_id)
    workspace_root = Path(workspace_root or Path.cwd()).resolve()
    _plugin_configs = plugin_configs or {}

    # 1. Load configuration
    startup_config = load_system_config(paths, workspace_root)
    agent_config = startup_config.model_copy(deep=True)
    resolved_agent = agent_definition
    if resolved_agent is not None:
        apply_agent_definition(agent_config, resolved_agent)
        provider_name = agent_definition.provider or provider_name
    provider_name = provider_name or agent_config.provider
    policy_base_config = agent_config.model_copy(deep=True)
    user_context = load_user_context(paths)

    session_policy = load_session_policy(paths, session_id)
    agent_config.permissions = merge_permission_config(
        agent_config.permissions,
        session_policy.get("permissions"),
    )
    agent_config.sandbox = merge_sandbox_config(
        agent_config.sandbox,
        session_policy.get("sandbox"),
    )

    # Merge plugin configs from system config
    if agent_config.plugins:
        _plugin_configs = {**_plugin_configs, **agent_config.plugins}

    # Ensure session state directory
    session_paths = paths.session(session_id)
    session_preexisting = session_paths.root.exists()
    thread_preexisting = session_paths.has_thread(thread_id)

    # 2. Create CoreStateStore
    state_store = CoreStateStore.create(
        session_paths,
        thread_id=thread_id,
        workspace_root=str(workspace_root),
        provider=provider_name,
    )
    metadata = state_store.read_thread_metadata()
    stored_agent = str(metadata.get("agent") or "") or None
    stored_provider = str(metadata.get("provider") or "") or None
    stored_definition = metadata.get("agent_definition")
    if resolved_agent is None and isinstance(stored_definition, dict):
        resolved_agent = _restore_agent_definition(stored_definition)
    if (
        selected_agent is not None
        and stored_agent is not None
        and selected_agent != stored_agent
    ):
        raise ValueError(
            f"Thread {thread_id!r} belongs to Agent {stored_agent!r}, "
            f"not {selected_agent!r}"
        )
    if selected_agent is None and agent_definition is None:
        selected_agent = stored_agent

    # 3. Create empty core components
    hook_manager = HookManager()
    tool_registry = ToolRegistry()
    context_builder = ContextBuilder()
    agent_registry = AgentRegistry()
    hook_manager.register(
        HookStage.AFTER_TOOLS,
        make_tool_result_cache_hook(
            state_store,
            max_inline_chars=agent_config.tool_result_max_inline_chars,
            preview_chars=agent_config.tool_result_preview_chars,
        ),
    )
    _register_configured_hooks(agent_config, hook_manager)

    # 4. Register core base tools (always available)
    for tool, sandbox_mode in CORE_BASE_TOOLS:
        tool_registry.register(
            tool,
            sandbox_mode=sandbox_mode,
        )

    # 5. Create SandboxPolicy + PermissionSystem
    sandbox = SandboxPolicy(
        agent_config.sandbox,
        data_root=paths.data_dir,
        workspace_root=workspace_root,
        session_root=state_store.root,
    )
    permissions = PermissionSystem(agent_config.permissions)
    if parent_permission_system is not None:
        permissions = PermissionIntersection(parent_permission_system, permissions)
    background_tasks = BackgroundTaskManager(
        workspace_root=str(workspace_root),
        sandbox=sandbox,
    )
    for tool in background_tasks.tools:
        tool_registry.register(tool, sandbox_mode="host")

    parent_engine: Engine | None = None

    async def create_child_engine(
        definition: Any,
        child_thread_id: str,
        child_depth: int,
    ) -> Engine:
        child = await bootstrap(
            paths=paths,
            provider_name=definition.provider or provider_name,
            session_id=session_id,
            thread_id=child_thread_id,
            workspace_root=workspace_root,
            plugin_dirs=plugin_dirs,
            plugin_configs=plugin_configs,
            llm_override=llm_override,
            agent_definition=definition,
            parent_permission_system=permissions,
            parent_thread_id=thread_id,
            subagent_depth=child_depth,
        )
        if parent_engine is not None:
            child.set_client_event_sink(parent_engine.client_event_sink)
        return child

    subagents = SubagentManager(
        registry=agent_registry,
        session_paths=session_paths,
        parent_thread_id=thread_id,
        engine_factory=create_child_engine,
        depth=subagent_depth,
        max_concurrency=agent_config.max_concurrent_subagents,
    )

    # 6. Discover and load plugins. ``plugin_dirs=[]`` is a deliberate
    # No-plugin mode used by isolated core tests and pure-core embeddings.
    resolved_plugin_dirs = _resolve_plugin_dirs(
        plugin_dirs,
        workspace_plugin_dirs=agent_config.plugin_paths,
    )
    plugin_loader: PluginLoader | None = None
    disabled_plugins = set(agent_config.disabled_plugins)
    if subagent_depth > 0:
        disabled_plugins.update(_SUBAGENT_BLOCKED_PLUGINS)

    try:
        if resolved_plugin_dirs:
            plugin_loader = await _load_plugins(
                resolved_plugin_dirs,
                hook_manager,
                tool_registry,
                context_builder,
                state_store,
                _plugin_configs,
                agent_registry,
                workspace_root,
                disabled_plugins,
                subagents,
            )

        if selected_agent is None and resolved_agent is None:
            default_agent = agent_registry.get("default")
            if default_agent is not None and default_agent.mode != "subagent":
                selected_agent = default_agent.name

        if selected_agent is not None:
            registered_agent = agent_registry.get(selected_agent)
            if resolved_agent is None:
                if registered_agent is None or (
                    registered_agent.mode == "subagent" and subagent_depth == 0
                ):
                    raise ValueError(f"Unknown primary agent: {selected_agent}")
                resolved_agent = registered_agent
            elif resolved_agent.name != selected_agent:
                raise ValueError(
                    f"Stored Agent {resolved_agent.name!r} does not match "
                    f"{selected_agent!r}"
                )
            elif resolved_agent.mode == "subagent" and subagent_depth == 0:
                raise ValueError(f"Unknown primary agent: {selected_agent}")
            apply_agent_definition(agent_config, resolved_agent)
            policy_base_config = startup_config.model_copy(deep=True)
            apply_agent_definition(policy_base_config, resolved_agent)
            provider_name = resolved_agent.provider or provider_name
            permissions = PermissionSystem(agent_config.permissions)
            if parent_permission_system is not None:
                permissions = PermissionIntersection(
                    parent_permission_system, permissions
                )

        if thread_preexisting and stored_provider is not None:
            provider_name = stored_provider
        state_store.provider = provider_name
        agent_config.provider = provider_name
        provider_config = load_provider_config(paths, provider_name)
        if resolved_agent is not None:
            apply_agent_provider(provider_config, resolved_agent)
        state_store.write_thread_metadata({
            "agent": resolved_agent.name if resolved_agent is not None else "",
            "agent_definition": (
                asdict(resolved_agent) if resolved_agent is not None else None
            ),
            "provider": provider_name,
            "parent_thread_id": parent_thread_id,
            "workspace_root": str(workspace_root),
            "model": provider_config.model,
            "model_mode": provider_config.model_mode,
            "context_window": agent_config.max_context_tokens,
        })

        # 7. Create LLM client
        if llm_override is not None:
            llm = llm_override
        else:
            from xbotv2.llm.client import create_llm
            llm = create_llm(provider_config)

        # 8. Run ON_SESSION_INIT hooks (plugins discover skills/MCP tools here)
        from xbotv2.api.runtime import SessionInfo
        init_ctx = HookContext(
            stage=HookStage.ON_SESSION_INIT,
            state={},
            config=agent_config,
            tools=tool_registry,
            sandbox=sandbox,
            plugin_store=None,
            session=SessionInfo(
                session_id=session_id,
                thread_id=thread_id,
                workspace_root=str(workspace_root),
                provider=provider_name,
            ),
            emit=lambda e: None,
        )
        await hook_manager.run(
            HookStage.ON_SESSION_INIT,
            init_ctx,
            short_circuit=False,
        )

        # Apply tool filter AFTER session init so plugin-discovered tools
        # (skills, MCP) are registered before restrict runs.
        if resolved_agent is not None:
            apply_agent_tools(tool_registry, agent_config, resolved_agent)
        elif agent_config.tools:
            tool_registry.restrict(agent_config.tools)

        # 9. Build engine
        engine = Engine(
            llm=llm,
            tool_registry=tool_registry,
            hook_manager=hook_manager,
            state_store=state_store,
            context_builder=context_builder,
            sandbox_policy=sandbox,
            permission_system=permissions,
            workspace_root=str(workspace_root),
            config=agent_config,
            plugin_loader=plugin_loader,
            background_tasks=background_tasks,
            subagents=subagents,
            agent_registry=agent_registry,
            startup_config=policy_base_config,
            model=provider_config.model,
            model_mode=provider_config.model_mode,
            context_window=agent_config.max_context_tokens,
            llm_is_override=llm_override is not None,
            user_context=user_context,
            max_iterations=(
                resolved_agent.max_iterations
                if resolved_agent is not None
                and resolved_agent.max_iterations is not None
                else 50
            ),
        )
        parent_engine = engine
        return engine
    except BaseException as bootstrap_error:
        if plugin_loader is not None:
            try:
                await plugin_loader.unload_all()
            except BaseException as cleanup_error:
                bootstrap_error.add_note(
                    f"Plugin cleanup after bootstrap failure also failed: "
                    f"{cleanup_error!r}"
                )
        if not thread_preexisting:
            if not session_preexisting:
                shutil.rmtree(session_paths.root, ignore_errors=True)
            else:
                shutil.rmtree(state_store.paths.root, ignore_errors=True)
        raise


# ------------------------------------------------------------------
# Internal helpers
# ------------------------------------------------------------------

def _restore_agent_definition(data: dict[str, Any]) -> AgentDefinition:
    values = dict(data)
    for field_name in ("tools", "disabled_tools"):
        value = values.get(field_name)
        if isinstance(value, list):
            values[field_name] = tuple(str(item) for item in value)
    return AgentDefinition(**values)


def _validate_identifier(field: str, value: str) -> None:
    if not value or value in {".", ".."} or not _IDENTIFIER_RE.fullmatch(value):
        raise ValueError(
            f"{field} must be a non-empty identifier using letters, numbers, '.', '_', or '-'"
        )


def _new_session_id() -> str:
    return f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(2)}"


def _resolve_plugin_dirs(
    plugin_dirs: list[Path | str] | None,
    *,
    builtin_plugins_dir: Path | None = None,
    workspace_plugin_dirs: list[Path | str] | None = None,
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
    resolved = [builtin_dir] if builtin_dir.exists() else []
    resolved.extend(Path(path) for path in workspace_plugin_dirs or [])
    return resolved


async def _load_plugins(
    plugin_dirs: list[Path],
    hook_manager: HookManager,
    tool_registry: ToolRegistry,
    context_builder: ContextBuilder,
    state_store: CoreStateStore,
    plugin_configs: dict[str, dict[str, Any]],
    agent_registry: AgentRegistry,
    workspace_root: Path,
    disabled_plugins: set[str],
    agent_runtime: Any,
) -> PluginLoader:
    """Discover, load, and wire plugins."""
    loader = PluginLoader(
        plugin_dirs=plugin_dirs,
        state_store=state_store,
        hook_manager=hook_manager,
        tool_registry=tool_registry,
        context_builder=context_builder,
        plugin_configs=plugin_configs,
        agent_registry=agent_registry,
        workspace_root=workspace_root,
        disabled_plugins=disabled_plugins,
        agent_runtime=agent_runtime,
    )
    await loader.load()
    return loader


def _register_configured_hooks(agent_config: Any, hook_manager: HookManager) -> None:
    """Register trusted standalone hooks declared for startup."""
    for decl in getattr(agent_config, "hooks", []) or []:
        hook_manager.register(HookStage(decl.stage), _resolve_hook_target(decl))


def _resolve_hook_target(declaration: Any) -> Any:
    """Resolve a module or workspace script target without changing sys.path."""
    source, attr_name = declaration.target.split(":", 1)
    if source.endswith(".py") or "/" in source or "\\" in source:
        base_dir = getattr(declaration, "base_dir", None)
        if base_dir is None:
            raise ValueError(
                f"Relative hook script {source!r} requires a workspace hooks file"
            )
        path = (Path(base_dir) / source).resolve()
        try:
            path.relative_to(Path(base_dir).resolve())
        except ValueError as exc:
            raise ValueError("Hook script paths must stay inside .xbot") from exc
        if not path.is_file():
            raise FileNotFoundError(f"Hook script not found: {path}")
        spec = importlib.util.spec_from_file_location(
            f"xbotv2_workspace_hook_{abs(hash(path))}", path
        )
        if spec is None or spec.loader is None:
            raise ImportError(f"Cannot load hook script: {path}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    else:
        module = importlib.import_module(source)
    try:
        callback = getattr(module, attr_name)
    except AttributeError as exc:
        raise ImportError(
            f"Hook target {declaration.target!r} does not exist"
        ) from exc
    if not callable(callback):
        raise TypeError(f"Hook target {declaration.target!r} is not callable")
    return callback
