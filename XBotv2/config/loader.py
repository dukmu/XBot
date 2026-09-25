"""Resolve the application plugin tree.

The configuration layer deliberately stops at a validated, merged plugin
tree.  It must not know the fields owned by individual plugins; consumers
validate their own entry with the Pydantic model declared by that plugin.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, cast

from pydantic import JsonValue

from XBotv2.core.paths import RuntimePaths
from XBotv2.core.variables import RuntimeVariables
from XBotv2.loader import resolve_agent_tree
from XBotv2.loader.contracts import PluginTree
from XBotv2.loader.runtime import plugin_config_schema, validate_plugin_config




def load_plugin_tree(
    paths: RuntimePaths,
    workspace_root: Path | str,
    session_id: str | None = None,
    thread_id: str = "agent",
    extra_plugins: list[dict[str, JsonValue]] | None = None,
    plugin_dirs: list[Path | str] | None = None,
    is_subagent: bool = False,
    no_plugins: bool = False,
    include_global: bool = True,
    include_workspace: bool = True,
    include_session: bool = True,
) -> PluginTree:
    """Return the one resolved tree shared by startup and configuration.

    The returned entries contain only generic declaration data.  No plugin
    identifier or plugin-owned field is interpreted here.

    The two-pass variable contract:

    1. ``${env:NAME}`` references were expanded while the document loaded
       (``expand_env_refs`` over the plugin tree), before a session exists;
    2. once a session identity is known this loader expands every
       ``${name}`` runtime reference (``workspace``, ``session_id``,
       ``thread_id``, ...) in each plugin config with the session's
       ``RuntimeVariables``, so consumers receive plain values.
    """
    workspace = Path(workspace_root).resolve()
    tree = resolve_agent_tree(
        paths=paths,
        workspace_root=workspace,
        is_subagent=is_subagent,
        no_plugins=no_plugins,
        plugin_dirs=plugin_dirs,
        extra_plugins=extra_plugins,
        session_id=session_id,
        include_global=include_global,
        include_workspace=include_workspace,
        include_session=include_session,
    )
    if session_id is not None:
        variables = RuntimeVariables.for_thread(
            paths, workspace, paths.session(session_id).thread(thread_id)
        )
        tree = tree_with_expanded_configs(tree, variables)
    for entry in tree.entries:
        validate_plugin_config(
            plugin_config_schema(entry),
            entry.config,
        )
    return tree


def tree_with_expanded_configs(
    tree: PluginTree,
    variables: RuntimeVariables,
) -> PluginTree:
    """Expand every runtime reference in all plugin configs, once."""
    return PluginTree([
        replace(
            entry,
            config=cast(
                dict[str, JsonValue], variables.expand_config(entry.config)
            ),
        )
        for entry in tree.entries
    ])


__all__ = [
    "load_plugin_tree",
]
