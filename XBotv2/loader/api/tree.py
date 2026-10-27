"""Shared declarative plugin-tree resolution for application consumers."""

from __future__ import annotations

from pathlib import Path
from pydantic import JsonValue
from XBotv2.core.api.paths import RuntimePaths
from XBotv2.loader.api.contracts import PluginEntry, PluginOverlay, PluginTree


DEFAULT_TREE = Path(__file__).resolve().parents[2] / "xcore.yaml"


def resolve_agent_tree(
    *,
    paths: RuntimePaths,
    workspace_root: Path | str,
    no_plugins: bool,
    plugin_dirs: list[Path | str] | None,
    extra_plugins: list[dict[str, JsonValue]] | None,
    session_id: str | None = None,
    include_global: bool = True,
    include_workspace: bool = True,
    include_session: bool = True,
) -> PluginTree:
    """Resolve base, global, workspace, session, then memory overlays."""
    external_entries = _external_entries(plugin_dirs, frozenset())
    tree = PluginTree([*PluginTree.from_yaml(DEFAULT_TREE).entries, *external_entries])
    excluded = frozenset(entry.id for entry in tree.entries if no_plugins and entry.optional)
    tree = tree.excluding(excluded)

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
                (candidate / "impl" / "plugin.py").is_file()
            ):
                continue
            if candidate.name not in excluded:
                entries.append(PluginEntry(id=candidate.name, name=f"{candidate.name}.impl.plugin", optional=True))
    return entries




def load_process_tree(*, paths: RuntimePaths, profile: str = "server", overrides: PluginOverlay | None = None, plugin_dirs=()) -> PluginTree:
    tree = PluginTree([*PluginTree.from_yaml(DEFAULT_TREE).entries, *_external_entries(plugin_dirs, frozenset())])
    tree = _patch_file(tree, paths.config_dir / "plugins.yaml", frozenset(), True)
    # Carrier profile selects process facets; thread facets use scope overlays.
    entries = [entry for entry in tree.entries if (profile in (entry.profiles or ()) if profile == "client" else entry.profiles is None or profile in entry.profiles or "agent" in entry.profiles)]
    tree = PluginTree(entries)
    return tree.patched_with(overrides, allow_new=False) if overrides is not None else tree
