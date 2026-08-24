"""Resolve application plugin trees from bundled and external layers."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from XBotv2.loader import PluginTree


DEFAULT_TREE = Path(__file__).resolve().parents[1] / "xcore.yaml"
SUBAGENT_FORBIDDEN_PLUGINS = frozenset({"subagents"})
OPTIONAL_CAPABILITIES = frozenset({
    "goal",
    "todolist",
    "skills",
    "mcp_plugin",
    "compact",
    "subagents",
    "browser",
    "token_manager",
})


def load_agent_tree(
    *,
    paths: Any,
    workspace_root: Path | str,
    is_subagent: bool,
    plugin_dirs: list[Path | str] | None,
    extra_plugins: list[dict[str, Any]] | None,
) -> PluginTree:
    """Load the bundled Agent tree and apply external configuration layers."""
    disabled = SUBAGENT_FORBIDDEN_PLUGINS if is_subagent else frozenset()
    values = {"disabled": disabled}
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
    workspace_plugins = Path(workspace_root) / ".xbot" / "plugins.yaml"
    if workspace_plugins.exists():
        tree = tree.merged_with(
            PluginTree.from_yaml(workspace_plugins, values=values)
        )
    return tree.for_profile("agent")


def load_server_tree(*, paths: Any) -> PluginTree:
    """Load the declarative server application profile."""
    tree = PluginTree.from_yaml(DEFAULT_TREE)
    plugins_file = paths.config_dir / "plugins.yaml"
    if plugins_file.exists():
        tree = tree.merged_with(PluginTree.from_yaml(plugins_file))
    selected = tree.for_profile("server")
    if not any(entry.id == "llm" for entry in selected.entries):
        raise ValueError("server application requires the llm profile entry")
    return selected


def load_acp_tree(*, paths: Any) -> PluginTree:
    """Load the ACP carrier application profile."""
    tree = PluginTree.from_yaml(DEFAULT_TREE)
    plugins_file = paths.config_dir / "plugins.yaml"
    if plugins_file.exists():
        tree = tree.merged_with(PluginTree.from_yaml(plugins_file))
    return tree.for_profile("acp")


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


__all__ = [
    "DEFAULT_TREE",
    "load_agent_tree",
    "load_server_tree",
    "load_acp_tree",
]
