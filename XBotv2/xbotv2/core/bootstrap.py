"""Bootstrap the complete XBotv2 runtime from configuration.

Sequence:
1. Load global configuration and startup-only workspace overlays
2. Create CoreStateStore
3. Create empty HookManager, ToolRegistry, ContextBuilder
4. Register core and configured workspace tools
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

import xcore

from xbotv2.config.loader import load_provider_config, load_runtime_config, load_user_context
from xbotv2.config.models import HookConfig, RuntimeConfig, WorkspaceToolConfig
from xbotv2.api.agents import AgentDefinition
from xbotv2.api.jobs import JobKind, JobRegistry
from xbotv2.api.paths import RuntimePaths
from xbotv2.api.runtime import SessionInfo
from xbotv2.api.tools import Tool
from xbotv2.api.variables import RuntimeVariables
from xbotv2.core.context import ContextBuilder
from xbotv2.core.agents import (
    AgentRegistry,
    EngineAgentRuntime,
    apply_agent_definition,
    apply_agent_provider,
    apply_agent_tools,
)
from xbotv2.core.builtin_tools.shell import SHELL_TOOLS
from xbotv2.core.engine import DEFAULT_MAX_ITERATIONS, Engine
from xbotv2.api.hooks import HookContext, HookStage
from xbotv2.components import (
    EngineComponent,
    HooksComponent,
    RuntimeComponent,
    ToolsComponent,
)
from xbotv2.plugin.bridge import plugin_runtime_for
from xbotv2.llm.base import BaseProvider
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
from xbotv2.core.builtin_tools.shell import SHELL_TOOLS
from xbotv2.core.builtin_tools.content import content_read_tool
from xbotv2.core.builtin_tools.interaction import (
    ask_user,
    request_permission,
    send_message,
)
from xbotv2.tools.result_cache import make_tool_result_cache_hook

_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9._-]+$")
NON_INTERACTIVE_FORBIDDEN_TOOLS = frozenset({"ask_user", "request_permission"})
SUBAGENT_FORBIDDEN_TOOLS = frozenset({
    "spawn_subagent",
    "list_subagents",
    "wait_subagent",
    "read_subagent",
    "cancel_subagent",
})
SUBAGENT_FORBIDDEN_PLUGINS = frozenset({"agents"})

# (tool, sandbox_mode)
CORE_BASE_TOOLS = [
    *((tool, "sandboxed") for tool in FILESYSTEM_TOOLS),
    *(
        (tool, "sandboxed" if tool.name in {"shell", "start_shell"} else "host")
        for tool in SHELL_TOOLS
    ),
    (content_read_tool, "sandboxed"),
    (send_message, "host"),
    (ask_user, "host"),
    (request_permission, "host"),
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
    llm_override: BaseProvider | None = None,
    selected_agent: str | None = None,
    agent_definition: AgentDefinition | None = None,
    parent_permission_system: (
        PermissionSystem | PermissionIntersection | None
    ) = None,
    parent_thread_id: str = "",
    is_subagent: bool = False,
    interactive: bool = True,
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

    # 1. Load configuration
    agent_config = load_runtime_config(paths, workspace_root, session_id)
    resolved_agent = agent_definition
    if provider_name == "default":
        provider_name = agent_config.provider
    if resolved_agent is not None:
        apply_agent_definition(agent_config, resolved_agent)
        provider_name = agent_definition.provider or provider_name
    user_context = load_user_context(paths)

    resolved_plugin_configs = {
        **agent_config.plugin_configs,
        **(plugin_configs or {}),
    }

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
    runtime_variables = RuntimeVariables.for_thread(
        paths,
        workspace_root,
        state_store.paths,
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

    # 3. Create the XCore plugin context and assemble the runtime components.
    #    Core capabilities (hooks/tools/runtime info) are XCore services
    #    provided by component packages; the engine itself is a component
    #    mounted after the builtin plugins (see ``xbotv2/components/``).
    plugin_ctx = xcore.Context(data_dir=state_store.paths.state_dir)
    tool_registry = ToolRegistry()
    context_builder = ContextBuilder()
    agent_registry = AgentRegistry()
    sandbox = SandboxPolicy(
        agent_config.sandbox,
        data_root=paths.data_dir,
        workspace_root=workspace_root,
        session_root=state_store.root,
        variables=runtime_variables,
    )
    permissions = PermissionSystem(
        agent_config.permissions,
        variables=runtime_variables,
    )
    if parent_permission_system is not None:
        permissions = PermissionIntersection(parent_permission_system, permissions)
    job_registry = JobRegistry(
        limits={
            JobKind.SUBAGENT: agent_config.max_concurrent_subagents,
        },
    )

    plugin_ctx.plugin(HooksComponent(plugin_runtime_factory=plugin_runtime_for))
    plugin_ctx.plugin(ToolsComponent(
        tool_registry=tool_registry,
        context_builder=context_builder,
        sandbox_policy=sandbox,
        permissions=permissions,
        job_registry=job_registry,
        agent_registry=agent_registry,
    ))
    plugin_ctx.plugin(RuntimeComponent(
        paths=paths,
        session=SessionInfo(
            session_id=session_id,
            thread_id=thread_id,
            workspace_root=str(workspace_root),
            provider=provider_name,
        ),
        workspace_root=workspace_root,
        data_root=state_store.paths.runtime.data_dir,
        variables=runtime_variables,
        runtime_config=agent_config,
        state_store=state_store,
    ))
    await plugin_ctx.start()

    # 4. Register core hooks and base tools on the shared components.
    hook_manager = plugin_ctx.hooks
    hook_manager.register(
        HookStage.AFTER_TOOLS,
        make_tool_result_cache_hook(
            state_store,
            max_inline_chars=agent_config.tool_results.max_inline_chars,
            preview_chars=agent_config.tool_results.preview_chars,
        ),
    )
    _register_configured_hooks(agent_config, hook_manager)
    for tool, sandbox_mode in CORE_BASE_TOOLS:
        if not interactive and tool.name in NON_INTERACTIVE_FORBIDDEN_TOOLS:
            continue
        tool_registry.register(
            tool,
            sandbox_mode=sandbox_mode,
        )
    _register_workspace_tools(agent_config, tool_registry)

    parent_engine: Engine | None = None

    async def create_child_engine(
        definition: AgentDefinition,
        child_thread_id: str,
        background: bool,
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
            is_subagent=True,
            interactive=interactive and not background,
        )
        if parent_engine is not None and not background:
            child.set_client_event_sink(parent_engine.client_event_sink)
        return child

    agent_runtime = (
        None
        if is_subagent
        else EngineAgentRuntime(
            registry=agent_registry,
            session_paths=session_paths,
            parent_thread_id=thread_id,
            engine_factory=create_child_engine,
        )
    )

    if agent_runtime is not None:
        plugin_ctx.set("agent_runtime", agent_runtime)

    # 6. Discover and load plugins. ``plugin_dirs=[]`` is a deliberate
    # No-plugin mode used by isolated core tests and pure-core embeddings.
    resolved_plugin_dirs = _resolve_plugin_dirs(
        plugin_dirs,
        workspace_plugin_dirs=agent_config.plugin_paths,
    )
    plugin_loader: PluginLoader | None = None
    disabled_plugins = set(agent_config.disabled_plugins)
    if is_subagent:
        disabled_plugins.update(SUBAGENT_FORBIDDEN_PLUGINS)

    try:
        if resolved_plugin_dirs:
            plugin_loader = PluginLoader(
                ctx=plugin_ctx,
                plugin_dirs=resolved_plugin_dirs,
                plugin_configs=resolved_plugin_configs,
                disabled_plugins=disabled_plugins,
                workspace_root=workspace_root,
            )
            await plugin_loader.load()

        # 7. Assemble the engine: an XCore component mounted after the
        #    builtin plugins (agent resolution, LLM, ON_SESSION_INIT hooks,
        #    tool filtering, and Engine construction all happen inside its
        #    ``apply``, reading capabilities from the context's services).
        engine_handle = plugin_ctx.plugin(EngineComponent(
            session_id=session_id,
            thread_id=thread_id,
            workspace_root=str(workspace_root),
            provider_name=provider_name,
            agent_config=agent_config,
            agent_definition=resolved_agent,
            llm_override=llm_override,
            selected_agent=selected_agent,
            parent_permission_system=parent_permission_system,
            parent_thread_id=parent_thread_id,
            is_subagent=is_subagent,
            interactive=interactive,
            user_context=user_context,
            plugin_loader=plugin_loader,
            context_builder=context_builder,
            thread_preexisting=thread_preexisting,
            stored_provider=stored_provider,
        ))
        await engine_handle
        engine = plugin_ctx.engine
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


def _register_configured_hooks(
    agent_config: RuntimeConfig,
    hook_manager: HookManager,
) -> None:
    """Register trusted standalone hooks declared for startup."""
    for decl in agent_config.hooks:
        hook_manager.register(HookStage(decl.stage), _resolve_hook_target(decl))


def _register_workspace_tools(
    agent_config: RuntimeConfig,
    tool_registry: ToolRegistry,
) -> None:
    """Register Tool exports explicitly declared by workspace configuration."""
    for declaration in agent_config.workspace_tools:
        exported = _resolve_workspace_target(declaration, directory="tools")
        tools = exported if isinstance(exported, (list, tuple)) else (exported,)
        if not tools:
            raise ValueError(f"Workspace Tool export is empty: {declaration.target}")
        for tool in tools:
            if not isinstance(tool, Tool):
                raise TypeError(
                    f"Workspace Tool export {declaration.target!r} must contain "
                    "xbotv2.api.Tool values"
                )
            tool_registry.register(tool, namespace="workspace", sandbox_mode="host")


def _resolve_hook_target(declaration: HookConfig) -> Any:
    """Resolve a module or workspace script target without changing sys.path."""
    source, attr_name = declaration.target.split(":", 1)
    if source.endswith(".py") or "/" in source or "\\" in source:
        callback = _resolve_workspace_target(declaration, directory="hooks")
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


def _resolve_workspace_target(
    declaration: HookConfig | WorkspaceToolConfig,
    *,
    directory: str,
) -> Any:
    """Load one declared export from a standard workspace extension directory."""
    source, attr_name = declaration.target.split(":", 1)
    base_dir = declaration.base_dir
    if base_dir is None:
        raise ValueError(
            f"Workspace {directory} target {source!r} must be declared in .xbot/config.yaml"
        )
    extension_dir = (Path(base_dir) / directory).resolve()
    path = (Path(base_dir) / source).resolve()
    try:
        path.relative_to(extension_dir)
    except ValueError as exc:
        raise ValueError(
            f"Workspace {directory} scripts must stay inside .xbot/{directory}"
        ) from exc
    if not path.is_file():
        raise FileNotFoundError(f"Workspace {directory} script not found: {path}")
    spec = importlib.util.spec_from_file_location(
        f"xbotv2_workspace_{directory}_{abs(hash(path))}", path
    )
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load workspace {directory} script: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    try:
        return getattr(module, attr_name)
    except AttributeError as exc:
        raise ImportError(
            f"Workspace target {declaration.target!r} does not exist"
        ) from exc
