"""Assemble the XBot runtime as an XCore application (composition root).

The composition root is deliberately thin: it resolves the session identity
and the assembly-time facts the tree needs (provider default, per-plugin
config, disabled flags), builds the declarative tree (``xcore.yaml`` +
user ``plugins.yaml`` + external plugin dirs) with ``${name}`` runtime
values, and mounts the loader on a fresh XCore context.  Everything else —
the state store (persistence plugin), thread metadata recovery and agent
resolution (agentloop plugin), config service (config plugin), subagent
factory (a bootstrap closure injected into the session plugin) — is owned by
the plugins themselves (design: ``XCore/docs/05-migration-plan.md``).
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

from XBotv2.core.agents import AgentDefinition
from XBotv2.core.runtime import SessionInfo
from XBotv2.loader import LoaderComponent, PluginTree

DEFAULT_TREE = Path(__file__).resolve().parent / "xcore.yaml"

SUBAGENT_FORBIDDEN_PLUGINS = frozenset({"agents"})

# Capability plugins excluded by --no-plugins.  workspace_instructions is a
# core workspace-extension component (AGENTS.md + workspace overlay), like
# coretools, so it is never excluded.
BUILTIN_PLUGINS = [
    "goal",
    "todolist",
    "skills",
    "mcp",
    "compact",
    "agents",
    "browser",
    "token_manager",
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
    llm_override=None,
    selected_agent: str | None = None,
    agent_definition: AgentDefinition | None = None,
    parent_permission_system=None,
    parent_thread_id: str = "",
    is_subagent: bool = False,
    interactive: bool = True,
    extra_plugins: list[dict[str, Any]] | None = None,
    exclude_plugins: set[str] | None = None,
    return_context: bool = False,
) -> Any:
    """Assemble the XBot runtime on an XCore context.

    Returns the Engine (or the XCore context when ``return_context`` is set,
    for server-style roots).  ``extra_plugins`` appends tree entries (e.g. the
    protocol server); ``exclude_plugins`` removes default entries (e.g. the
    agent loop for a server root)."""
    _validate_identifier("provider_name", provider_name)
    session_id = session_id or _new_session_id()
    _validate_identifier("session_id", session_id)
    _validate_identifier("thread_id", thread_id)
    workspace_root = Path(workspace_root or Path.cwd()).resolve()

    # 1. Session identity (filesystem roots the tree references).  All plugin
    #    configuration lives in xcore.yaml; runtime initialization (state
    #    store, thread metadata recovery, agent resolution, provider default)
    #    is owned by the persistence / agentloop plugins, not the root.
    session_paths = paths.session(session_id)
    session_preexisting = session_paths.root.exists()
    thread_preexisting = session_paths.has_thread(thread_id)
    thread_paths = session_paths.thread(thread_id)

    resolved_agent = agent_definition
    disabled_plugins = SUBAGENT_FORBIDDEN_PLUGINS if is_subagent else set()

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
        session_id=session_id,
        thread_id=thread_id,
        workspace_root=workspace_root,
        provider_name=provider_name,
        resolved_agent=resolved_agent,
        llm_override=llm_override,
        selected_agent=selected_agent,
        parent_permission_system=parent_permission_system,
        parent_thread_id=parent_thread_id,
        interactive=interactive,
        is_subagent=is_subagent,
        plugin_dirs=plugin_dirs,
        disabled_plugins=disabled_plugins,
        include_builtins=plugin_dirs is None,
        session_paths=session_paths,
        engine_factory=create_child_engine,
        extra_plugins=extra_plugins,
        exclude_plugins=exclude_plugins,
    )

    # 4. Create the XCore context, mount the loader, and load the tree.
    plugin_ctx = xcore.Context(data_dir=thread_paths.state_dir)
    try:
        loader_handle = plugin_ctx.plugin(LoaderComponent(tree))
        await plugin_ctx.start()
        await plugin_ctx.loader.load()

        if return_context:
            return plugin_ctx
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
                shutil.rmtree(thread_paths.root, ignore_errors=True)
        raise


# ------------------------------------------------------------------
# Plugin tree assembly (declarative xcore.yaml + dynamic injection)
# ------------------------------------------------------------------


def _build_plugin_tree(
    *,
    paths,
    session_id: str,
    thread_id: str,
    workspace_root: Path,
    provider_name: str,
    resolved_agent: AgentDefinition | None,
    llm_override,
    selected_agent: str | None,
    parent_permission_system,
    parent_thread_id: str,
    interactive: bool,
    is_subagent: bool,
    plugin_dirs: list[Path | str] | None,
    disabled_plugins: set[str],
    include_builtins: bool,
    session_paths: Any,
    engine_factory: Any,
    extra_plugins: list[dict[str, Any]] | None,
    exclude_plugins: set[str] | None,
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
        "session_id": session_id,
        "thread_id": thread_id,
        "workspace_root": workspace_root,
        "provider_name": provider_name,
        "agent_definition": resolved_agent,
        "engine_factory": engine_factory,
        "parent_thread_id": parent_thread_id,
        "parent_permission_system": parent_permission_system,
        "interactive": interactive,
        "is_subagent": is_subagent,
        "selected_agent": selected_agent,
        "llm_override": llm_override,
        "disabled": disabled_plugins,
    }
    tree = PluginTree.from_yaml(DEFAULT_TREE, values=values)
    if not include_builtins:
        tree = tree.excluding(set(BUILTIN_PLUGINS))

    external_entries = _plugin_dirs_to_entries(plugin_dirs, disabled_plugins)
    if exclude_plugins:
        tree = tree.excluding(set(exclude_plugins))
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
    if extra_plugins:
        tree = tree.merged_with(PluginTree.from_dict(extra_plugins))

    # Optional cordis.yaml-style user tree (last write wins per id) at
    # ~/.xbot/config/plugins.yaml.  Workspace overlays are applied by the
    # workspace_instructions plugin, not the composition root.
    plugins_file = paths.config_dir / "plugins.yaml"
    if plugins_file.exists():
        tree = tree.merged_with(PluginTree.from_yaml(plugins_file, values=values))
    return tree


def _plugin_dirs_to_entries(
    plugin_dirs: list[Path | str] | None,
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
                "config": {},
            })
    return entries


# ------------------------------------------------------------------
# Internal helpers
# ------------------------------------------------------------------


def _validate_identifier(field: str, value: str) -> None:
    if not value or value in {".", ".."} or not _IDENTIFIER_RE.fullmatch(value):
        raise ValueError(
            f"{field} must be a non-empty identifier using letters, numbers, '.', '_', or '-'"
        )


def _new_session_id() -> str:
    return f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(2)}"
