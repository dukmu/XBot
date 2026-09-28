"""HTTP server application startup."""

from __future__ import annotations

from pydantic import JsonValue
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
    provider_name: str | None,
    workspace_root: str | None,
    no_plugins: bool | None,
) -> Context:
    """Start the server host without constructing an Agent session."""
    ensure_initial_config(paths)
    session_config: dict[str, JsonValue] = {}
    if workspace_root is not None:
        session_config["workspace_root"] = workspace_root
    if provider_name is not None:
        session_config["provider_name"] = provider_name
    if no_plugins is not None:
        session_config["no_plugins"] = no_plugins
    tree = load_server_tree(paths=paths, overrides=PluginOverlay.parse([
        {"id": "session", "config": session_config},
    ]))

    ctx = Context(data_dir=paths.data_dir)
    ctx.set("runtime_paths", paths)
    ctx.set("agent_application_factory", create_agent_application)
    return await boot_application(
        ctx=ctx,
        tree=tree,
    )


__all__ = ["start_server_application"]
