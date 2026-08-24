"""Materialize the editable global configuration overlay."""

from __future__ import annotations

from importlib.resources import files
from pathlib import Path

_INITIAL_PLUGINS_YAML = """\
# Global user plugin tree overlay for this data directory.
# The bundled XBotv2/xcore.yaml is the base tree; entries here are merged
# over it (same-id entries retain omitted fields and deep-merge config; new
# entries require name). Workspace overlays take precedence and live in
# <workspace>/.xbot/plugins.yaml. Session overrides are applied last.
# Example:
# - id: agents
#   disabled: true
# - id: llm
#   config:
#     default: minimax
"""

_BUILTIN_SKILL = "xbot-plugin-development"


def _copy_resource_tree(source, target: Path) -> None:
    """Copy package resources without requiring the wheel to be unpacked."""
    target.mkdir(parents=True, exist_ok=True)
    for child in source.iterdir():
        destination = target / child.name
        if child.is_dir():
            _copy_resource_tree(child, destination)
        else:
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(child.read_bytes())


def ensure_builtin_skills(paths) -> None:
    """Materialize skills shipped inside the XBot distribution.

    ``paths.data_dir`` is the authority (it defaults to ``~/.xbot`` but may be
    set by ``--data-dir``/``XBOT_DATA_DIR``). Only files owned by the bundled
    skill are updated; unrelated user skills in the same directory remain
    untouched.
    """
    source = files("XBotv2").joinpath(".agents", "skills", _BUILTIN_SKILL)
    target = paths.data_dir / ".agents" / "skills" / _BUILTIN_SKILL
    _copy_resource_tree(source, target)


def ensure_initial_config(paths) -> None:
    """Write global defaults and bundled skills on first use."""
    paths.config_dir.mkdir(parents=True, exist_ok=True)
    plugins_file = paths.config_dir / "plugins.yaml"
    if not plugins_file.exists():
        plugins_file.write_text(_INITIAL_PLUGINS_YAML, encoding="utf-8")
    ensure_builtin_skills(paths)


__all__ = ["ensure_builtin_skills", "ensure_initial_config"]
