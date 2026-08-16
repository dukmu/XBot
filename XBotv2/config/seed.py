"""Initial global configuration written on first run.

Like DSH materializes its profile root at boot, XBot writes the initial
global user tree ``config/plugins.yaml`` into the data directory when it is
missing.  Provider definitions and the user context are plugin tree config
(the ``llm`` and ``config`` entries of the bundled ``xcore.yaml``), so the
only seeded document is the user overlay users edit instead of the bundled
tree.
"""

from __future__ import annotations

_INITIAL_PLUGINS_YAML = """\
# Global user plugin tree overlay for this data directory.
# The bundled XBotv2/xcore.yaml is the base tree; entries here are merged
# over it (same-id entries deep-merge config, new entries mount).  Workspace
# overlays live in <workspace>/.xbot/plugins.yaml instead.
# Example:
# - id: agents
#   disabled: true
# - id: llm
#   config:
#     default: minimax
plugins: []
"""


def ensure_initial_config(paths) -> None:
    """Write the initial global config files when missing (first run)."""
    config_dir = paths.config_dir
    if not config_dir.is_dir():
        config_dir.mkdir(parents=True, exist_ok=True)
    path = config_dir / "plugins.yaml"
    if not path.exists():
        path.write_text(_INITIAL_PLUGINS_YAML, encoding="utf-8")


__all__ = ["ensure_initial_config"]
