"""Initial global configuration written on first run.

Like DSH materializes its profile root at boot, XBot writes the initial
global config documents into the data directory when they are missing:
``config/plugins.yaml`` (the global user tree overlay), ``config/providers.yaml``
and ``config/user.yaml`` (documented templates).  All three are no-ops when
absent: an empty tree overlay merges nothing, and a comment-only providers
document resolves to the same defaults as a missing file.  Users edit these
files instead of the bundled ``xcore.yaml``.
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
plugins: []
"""

_INITIAL_PROVIDERS_YAML = """\
# Provider definitions for this data directory.
# Each entry: provider (openai|deepseek|lmstudio-openai|anthropic|lmstudio|mock),
# model, optional base_url, api_key or api_key_env, and generation limits.
# `default:` names the provider used when none is selected.  Uncomment and
# adapt the providers you use:
# default: minimax
# providers:
#   minimax:
#     provider: anthropic
#     model: Minimax-M3
#     base_url: https://api.minimaxi.com/anthropic
#     api_key_env: MINIMAX_API_TOKEN
#     max_context_tokens: 204800
#     max_output_tokens: 32768
"""

_INITIAL_USER_YAML = """\
user_id: default-user
user_name: User
platform: terminal
session_type: interactive
"""


def ensure_initial_config(paths) -> None:
    """Write the initial global config files when missing (first run)."""
    config_dir = paths.config_dir
    if not config_dir.is_dir():
        config_dir.mkdir(parents=True, exist_ok=True)
    for name, content in (
        ("plugins.yaml", _INITIAL_PLUGINS_YAML),
        ("providers.yaml", _INITIAL_PROVIDERS_YAML),
        ("user.yaml", _INITIAL_USER_YAML),
    ):
        path = config_dir / name
        if not path.exists():
            path.write_text(content, encoding="utf-8")


__all__ = ["ensure_initial_config"]
