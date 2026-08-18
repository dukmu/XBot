"""Materialize the editable global configuration overlay."""

from __future__ import annotations

_INITIAL_PLUGINS_YAML = """\
# Global user plugin tree overlay for this data directory.
# The bundled XBotv2/xcore.yaml is the base tree; entries here are merged
# over it (same-id entries deep-merge config, new entries mount). Workspace
# overlays live in <workspace>/.xbot/plugins.yaml instead.
# Example:
# - id: agents
#   disabled: true
# - id: llm
#   config:
#     default: minimax
"""


def ensure_initial_config(paths) -> None:
    """Write the global plugin overlay on first use."""
    paths.config_dir.mkdir(parents=True, exist_ok=True)
    plugins_file = paths.config_dir / "plugins.yaml"
    if not plugins_file.exists():
        plugins_file.write_text(_INITIAL_PLUGINS_YAML, encoding="utf-8")


__all__ = ["ensure_initial_config"]
