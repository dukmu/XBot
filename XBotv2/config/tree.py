"""Resolve the plugin tree from the bundled configuration and user overlays."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from XBotv2.loader import PluginEntry, PluginTree


DEFAULT_TREE = Path(__file__).resolve().parents[1] / "xcore.yaml"
SUBAGENT_FORBIDDEN_PLUGINS = frozenset({"agents"})
OPTIONAL_CAPABILITIES = frozenset({
    "goal", "todolist", "skills", "mcp", "compact", "agents", "browser",
    "token_manager",
})


def load_agent_tree(
    *,
    paths: Any,
    session_paths: Any,
    session_id: str,
    thread_id: str,
    workspace_root: Path,
    provider_name: str,
    parent_permission_system: Any,
    interactive: bool,
    is_subagent: bool,
    plugin_dirs: list[Path | str] | None,
    extra_plugins: list[dict[str, Any]] | None,
) -> PluginTree:
    """Load the bundled Agent tree and apply external configuration layers."""
    disabled = SUBAGENT_FORBIDDEN_PLUGINS if is_subagent else frozenset()
    values = {
        "paths": paths,
        "session_paths": session_paths,
        "session_id": session_id,
        "thread_id": thread_id,
        "workspace_root": workspace_root,
        "provider_name": provider_name,
        "parent_permission_system": parent_permission_system,
        "interactive": interactive,
        "disabled": disabled,
    }
    tree = PluginTree.from_yaml(DEFAULT_TREE, values=values)
    if plugin_dirs is not None:
        tree = tree.excluding(set(OPTIONAL_CAPABILITIES))

    entries = _external_entries(plugin_dirs, disabled)
    if entries:
        tree = tree.merged_with(PluginTree.from_dict(entries))
    if extra_plugins:
        tree = tree.merged_with(PluginTree.from_dict(extra_plugins))

    plugins_file = paths.config_dir / "plugins.yaml"
    if plugins_file.exists():
        tree = tree.merged_with(PluginTree.from_yaml(plugins_file, values=values))
    return tree


def load_server_tree(
    *,
    paths: Any,
    provider_name: str,
    workspace_root: str,
    no_plugins: bool,
) -> PluginTree:
    """Load the provider directory and HTTP host tree."""
    tree = PluginTree.from_yaml(DEFAULT_TREE)
    plugins_file = paths.config_dir / "plugins.yaml"
    if plugins_file.exists():
        tree = tree.merged_with(PluginTree.from_yaml(plugins_file))
    llm = next((entry for entry in tree.entries if entry.id == "llm"), None)
    if llm is None:
        raise ValueError("server application requires the llm profile entry")
    server = PluginEntry(
        id="server",
        name="server",
        inject=["llm"],
        config={
            "paths": paths,
            "provider_name": provider_name,
            "workspace_root": workspace_root,
            "no_plugins": no_plugins,
        },
    )
    return PluginTree([llm, server])


def _external_entries(
    plugin_dirs: list[Path | str] | None,
    disabled: frozenset[str],
) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for plugin_dir in plugin_dirs or []:
        root = Path(plugin_dir)
        if not root.exists():
            continue
        for candidate in sorted(root.iterdir()):
            if not candidate.is_dir() or not (
                (candidate / "plugin.py").exists()
                or (candidate / "__init__.py").exists()
            ):
                continue
            if candidate.name not in disabled:
                entries.append({
                    "id": candidate.name,
                    "name": candidate.name,
                    "config": {},
                })
    return entries


__all__ = ["DEFAULT_TREE", "load_agent_tree", "load_server_tree"]
