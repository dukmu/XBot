"""Assemble the XBot runtime as an XCore application (composition root).

The runtime is declared declaratively: the bundled ``xcore.yaml`` tree (a
cordis.yaml-style mechanism, like DeepSeek Harness's ``cordis.patch.yml``)
lists every plugin with its static configuration; the user's
``data/config/plugins.yaml`` overrides or appends entries; bootstrap injects
the session-dynamic values (paths, workspace, state store, provider,
child-engine factory) by entry id; then the loader component is mounted on a
fresh XCore context and the tree is loaded.  Everything else — events, tools,
state, the engine — is provided by plugins/services on the context (design:
``XCore/docs/05-migration-plan.md``).
"""

from __future__ import annotations

import secrets
import shutil
import sys
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any

import xcore

from XBotv2.config.loader import load_runtime_config, load_user_context
from XBotv2.config.models import RuntimeConfig
from XBotv2.agentloop.agents import apply_agent_definition
from XBotv2.core.agents import AgentDefinition
from XBotv2.core.runtime import SessionInfo
from XBotv2.loader import LoaderComponent, PluginTree
from XBotv2.persistence.store import CoreStateStore

DEFAULT_TREE = Path(__file__).resolve().parent / "xcore.yaml"

SUBAGENT_FORBIDDEN_PLUGINS = frozenset({"agents"})

BUILTIN_PLUGINS = [
    "goal",
    "todolist",
    "skills",
    "mcp",
    "compact",
    "agents",
    "browser",
    "token_manager",
    "workspace_instructions",
]

_IDENTIFIER_RE = __import__("re").compile(r"^[A-Za-z0-9._-]+$")


async def bootstrap(
    *,
    paths,
    provider_name: str = "default",
    session_id: str | None = None,
    thread_id: str = "agent",
    workspace_root: Path | str | None = None,
    plugin_dirs: list[Path | str] | None = None,
    plugin_configs: dict[str, dict[str, Any]] | None = None,
    llm_override=None,
    selected_agent: str | None = None,
    agent_definition: AgentDefinition | None = None,
    parent_permission_system=None,
    parent_thread_id: str = "",
    is_subagent: bool = False,
    interactive: bool = True,
) -> Any:
    """Assemble the XBot runtime on an XCore context; returns the Engine."""
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

    # 2. Session state
    session_paths = paths.session(session_id)
    session_preexisting = session_paths.root.exists()
    thread_preexisting = session_paths.has_thread(thread_id)
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

    disabled_plugins = set(agent_config.disabled_plugins)
    if is_subagent:
        disabled_plugins.update(SUBAGENT_FORBIDDEN_PLUGINS)

    parent_engine: Any | None = None

    async def create_child_engine(
        definition: AgentDefinition,
        child_thread_id: str,
        background: bool,
    ):
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
            parent_permission_system=None,  # resolved via ctx.permissions below
            parent_thread_id=thread_id,
            is_subagent=True,
            interactive=interactive and not background,
        )
        if parent_engine is not None and not background:
            child.set_client_event_sink(parent_engine.client_event_sink)
        return child

    _plugin_import_paths: list[str] = []
    for plugin_dir in plugin_dirs or []:
        root = Path(plugin_dir)
        if root.exists() and str(root) not in sys.path:
            sys.path.insert(0, str(root))
            _plugin_import_paths.append(str(root))

    # 3. Build the plugin tree from the declarative xcore.yaml + overlays.
    tree = _build_plugin_tree(
        paths=paths,
        state_store=state_store,
        session_id=session_id,
        thread_id=thread_id,
        workspace_root=workspace_root,
        provider_name=provider_name,
        agent_config=agent_config,
        resolved_agent=resolved_agent,
        llm_override=llm_override,
        selected_agent=selected_agent,
        parent_permission_system=parent_permission_system,
        parent_thread_id=parent_thread_id,
        interactive=interactive,
        user_context=user_context,
        plugin_configs=resolved_plugin_configs,
        plugin_dirs=plugin_dirs,
        disabled_plugins=disabled_plugins,
        include_builtins=plugin_dirs is None,
        thread_preexisting=thread_preexisting,
        stored_provider=stored_provider,
        session_paths=session_paths,
        engine_factory=create_child_engine,
    )

    # 4. Create the XCore context, mount the loader, and load the tree.
    plugin_ctx = xcore.Context(data_dir=state_store.paths.state_dir)
    try:
        loader_handle = plugin_ctx.plugin(LoaderComponent(tree))
        await plugin_ctx.start()
        await plugin_ctx.loader.load()

        engine = plugin_ctx.engine
        parent_engine = engine
        return engine
    except BaseException as bootstrap_error:
        for path in reversed(_plugin_import_paths):
            try:
                sys.path.remove(path)
            except ValueError:
                pass
        loader = plugin_ctx.get("loader")
        if loader is not None:
            try:
                await loader.unload_all()
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
# Plugin tree assembly (declarative xcore.yaml + dynamic injection)
# ------------------------------------------------------------------


def _build_plugin_tree(
    *,
    paths,
    state_store: CoreStateStore,
    session_id: str,
    thread_id: str,
    workspace_root: Path,
    provider_name: str,
    agent_config: RuntimeConfig,
    resolved_agent: AgentDefinition | None,
    llm_override,
    selected_agent: str | None,
    parent_permission_system,
    parent_thread_id: str,
    interactive: bool,
    user_context: Any,
    plugin_configs: dict[str, dict[str, Any]],
    plugin_dirs: list[Path | str] | None,
    disabled_plugins: set[str],
    include_builtins: bool,
    thread_preexisting: bool,
    stored_provider: str | None,
    session_paths: Any,
    engine_factory: Any,
) -> PluginTree:
    """Load the bundled tree with runtime values, merge overlays.

    The tree structure and static config live in ``xcore.yaml``; this function
    only supplies the session-dynamic values (identity, paths, state store,
    provider, child-engine factory, per-plugin config / disabled flags) that
    the file references as ``${name}`` — a DSH-style composition root, no
    hardcoded plugin list or per-entry injection here.
    """
    values: dict[str, Any] = {
        "paths": paths,
        "session_paths": session_paths,
        "state_store": state_store,
        "session_id": session_id,
        "thread_id": thread_id,
        "workspace_root": workspace_root,
        "provider_name": provider_name,
        "agent_config": agent_config,
        "agent_definition": resolved_agent,
        "user_context": user_context,
        "engine_factory": engine_factory,
        "parent_thread_id": parent_thread_id,
        "parent_permission_system": parent_permission_system,
        "interactive": interactive,
        "selected_agent": selected_agent,
        "llm_override": llm_override,
        "stored_provider": stored_provider,
        "thread_preexisting": thread_preexisting,
        "plugin_configs": plugin_configs,
        "disabled": disabled_plugins,
    }
    tree = PluginTree.from_yaml(DEFAULT_TREE, values=values)
    if not include_builtins:
        tree = tree.excluding(set(BUILTIN_PLUGINS))

    external_entries = _plugin_dirs_to_entries(
        plugin_dirs, plugin_configs, disabled_plugins
    )
    if external_entries:
        # External plugins mount before the engine (core mounts last).
        core_entry = next(
            (entry for entry in tree.entries if entry.id == "agentloop"), None
        )
        if core_entry is not None:
            rest = [entry for entry in tree.entries if entry.id != "agentloop"]
            tree = PluginTree(
                [*rest, *PluginTree.from_dict(external_entries).entries, core_entry]
            )
        else:
            tree = tree.merged_with(PluginTree.from_dict(external_entries))

    # Optional cordis.yaml-style user tree: entries override same-id entries
    # and new ids are appended.
    plugins_file = paths.config_dir / "plugins.yaml"
    if plugins_file.exists():
        user_tree = PluginTree.from_yaml(plugins_file, values=values)
        tree = tree.merged_with(user_tree)
    return tree


def _plugin_dirs_to_entries(
    plugin_dirs: list[Path | str] | None,
    plugin_configs: dict[str, dict[str, Any]],
    disabled_plugins: set[str],
) -> list[dict[str, Any]]:
    """Scan external plugin directories for ``<name>/plugin.py`` modules."""
    entries: list[dict[str, Any]] = []
    if plugin_dirs is None:
        return entries
    for plugin_dir in plugin_dirs:
        root = Path(plugin_dir)
        if not root.exists():
            continue
        for candidate in sorted(root.iterdir()):
            if not candidate.is_dir():
                continue
            if not (
                (candidate / "plugin.py").exists()
                or (candidate / "__init__.py").exists()
            ):
                continue
            name = candidate.name
            if name in disabled_plugins:
                continue
            entries.append({
                "id": name,
                "name": name,
                "config": dict(plugin_configs.get(name, {})),
            })
    return entries


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
