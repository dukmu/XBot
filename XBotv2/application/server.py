"""HTTP server application startup."""

from __future__ import annotations

from xcore import Context

from XBotv2.application.boot import boot_application
from XBotv2.application.app import create_agent_application
from XBotv2.application.tree import load_server_tree
from XBotv2.config.seed import ensure_initial_config
from XBotv2.loader import PluginOverlay
from XBotv2.core.paths import RuntimePaths


async def start_server_application(
    *,
    paths: RuntimePaths,
    overrides: PluginOverlay | None = None,
) -> Context:
    """Start the server host without constructing an Agent session."""
    ensure_initial_config(paths)
    tree = load_server_tree(paths=paths, overrides=overrides)

    ctx = Context(data_dir=paths.data_dir)
    ctx.set("runtime_paths", paths)
    ctx.set("agent_application_factory", create_agent_application)
    return await boot_application(
        ctx=ctx,
        tree=tree,
    )


__all__ = ["start_server_application"]
