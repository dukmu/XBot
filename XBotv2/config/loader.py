"""Resolve the application plugin tree.

The configuration layer deliberately stops at a validated, merged plugin
tree.  It must not know the fields owned by individual plugins; consumers
validate their own entry with the Pydantic model declared by that plugin.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from pydantic import JsonValue
from XBotv2.loader import resolve_agent_tree
from XBotv2.loader.contracts import PluginTree
from XBotv2.loader.runtime import plugin_config_schema, validate_plugin_config
from XBotv2.core.paths import RuntimePaths


_ENV = re.compile(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?")


def expand_env(value: str) -> str:
    """Expand environment references and reject missing variables."""
    if not isinstance(value, str):
        return value

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in os.environ:
            raise ValueError(f"Environment variable {name} is not set")
        return os.environ[name]

    return _ENV.sub(replace, value)


def load_plugin_tree(
    paths: RuntimePaths,
    workspace_root: Path | str,
    session_id: str | None = None,
    extra_plugins: list[dict[str, JsonValue]] | None = None,
    plugin_dirs: list[Path | str] | None = None,
    is_subagent: bool = False,
    no_plugins: bool = False,
) -> PluginTree:
    """Return the one resolved tree shared by startup and configuration.

    The returned entries contain only generic declaration data.  No plugin
    identifier or plugin-owned field is interpreted here.
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
    )
    for entry in tree.entries:
        validate_plugin_config(
            plugin_config_schema(entry),
            entry.config,
        )
    return tree


__all__ = [
    "expand_env",
    "load_plugin_tree",
]
