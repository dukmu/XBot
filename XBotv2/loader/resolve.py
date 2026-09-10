"""Shared declarative plugin-tree resolution for application consumers."""

from __future__ import annotations

from pathlib import Path
from pydantic import JsonValue

from XBotv2.core.paths import RuntimePaths
from XBotv2.loader.contracts import PluginEntry, PluginOverlay, PluginTree


DEFAULT_TREE = Path(__file__).resolve().parents[1] / "xcore.yaml"
SUBAGENT_FORBIDDEN_PLUGINS = frozenset({"subagents"})
OPTIONAL_CAPABILITIES = frozenset({
    "goal", "todolist", "skills", "mcp_plugin", "compact", "subagents",
    "browser", "token_manager", "workspace_instructions",
})


def resolve_agent_tree(
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
    """Resolve base, global, workspace, session, then memory overlays."""
    excluded = frozenset(
        (OPTIONAL_CAPABILITIES if no_plugins else frozenset())
        | (SUBAGENT_FORBIDDEN_PLUGINS if is_subagent else frozenset())
    )
    external_entries = _external_entries(plugin_dirs, excluded)
    if no_plugins:
        excluded |= frozenset(entry.id for entry in external_entries)
    tree = PluginTree.from_yaml(DEFAULT_TREE).excluding(excluded)
    if not no_plugins:
        tree = PluginTree([*tree.entries, *external_entries])

    if include_global:
        tree = _patch_file(tree, paths.config_dir / "plugins.yaml", excluded, not no_plugins)
    workspace = Path(workspace_root).resolve()
    if include_workspace:
        tree = _patch_file(tree, workspace / ".xbot" / "plugins.yaml", excluded, not no_plugins)
    if include_session and session_id is not None:
        tree = _patch_file(
            tree,
            paths.session(session_id).config_file,
            excluded,
            not no_plugins,
        )
    if extra_plugins:
        tree = tree.patched_with(
            PluginOverlay.parse(extra_plugins),
            excluded=excluded,
            allow_new=False,
        )
    return tree.for_profile("agent")


def _patch_file(
    tree: PluginTree,
    path: Path,
    excluded: frozenset[str],
    allow_new: bool,
) -> PluginTree:
    if not path.exists():
        return tree
    overlay = PluginOverlay.from_yaml(path)
    return tree.patched_with(overlay, excluded=excluded, allow_new=allow_new)


def _external_entries(
    plugin_dirs: list[Path | str] | None,
    excluded: frozenset[str],
) -> list[PluginEntry]:
    entries: list[PluginEntry] = []
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
            if candidate.name not in excluded:
                entries.append(PluginEntry(id=candidate.name, name=candidate.name))
    return entries


__all__ = ["DEFAULT_TREE", "resolve_agent_tree"]
