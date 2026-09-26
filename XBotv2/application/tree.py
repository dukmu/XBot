"""Resolve application plugin trees from bundled and external layers."""

from __future__ import annotations

from pathlib import Path
from pydantic import JsonValue

from XBotv2.loader import PluginOverlay, PluginTree
from XBotv2.loader.resolve import DEFAULT_TREE, resolve_agent_tree
from XBotv2.core.paths import RuntimePaths

def load_agent_tree(
    *,
    paths: RuntimePaths,
    workspace_root: Path | str,
    is_subagent: bool,
    no_plugins: bool,
    plugin_dirs: list[Path | str] | None,
    extra_plugins: list[dict[str, JsonValue]] | None,
    session_id: str | None = None,
    include_global: bool = True,
    include_workspace: bool = True,
    include_session: bool = True,
) -> PluginTree:
    """Resolve the Agent tree through the shared loader contract."""
    return resolve_agent_tree(
        paths=paths,
        workspace_root=workspace_root,
        is_subagent=is_subagent,
        no_plugins=no_plugins,
        plugin_dirs=plugin_dirs,
        extra_plugins=extra_plugins,
        session_id=session_id,
        include_global=include_global,
        include_workspace=include_workspace,
        include_session=include_session,
    )


def load_server_tree(*, paths: RuntimePaths) -> PluginTree:
    """Load the declarative server application profile."""
    selected = _load_carrier_tree(paths, "server")
    if not any(entry.id == "llm" for entry in selected.entries):
        raise ValueError("server application requires the llm profile entry")
    return selected


def load_acp_tree(*, paths: RuntimePaths) -> PluginTree:
    """Load the ACP carrier application profile."""
    return _load_carrier_tree(paths, "acp")


def load_client_tree(*, paths: RuntimePaths) -> PluginTree:
    """Load the local client-plugin profile from the common plugin tree."""
    return _load_carrier_tree(paths, "client")


def _load_carrier_tree(paths: RuntimePaths, profile: str) -> PluginTree:
    tree = PluginTree.from_yaml(DEFAULT_TREE)
    plugins_file = paths.config_dir / "plugins.yaml"
    if plugins_file.exists():
        tree = tree.patched_with(PluginOverlay.from_yaml(plugins_file))
    return tree.for_profile(profile)


__all__ = [
    "DEFAULT_TREE",
    "load_agent_tree",
    "load_client_tree",
    "load_server_tree",
    "load_acp_tree",
]
