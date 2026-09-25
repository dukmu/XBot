"""HTTP server application startup."""

from __future__ import annotations

from pathlib import Path
from xcore import Context

from XBotv2.application.boot import boot_application
from XBotv2.application.contracts import SessionLaunch
from XBotv2.application.app import create_agent_application
from XBotv2.application.tree import load_server_tree
from XBotv2.config.seed import ensure_initial_config
from XBotv2.server import ServerOptions
from XBotv2.core.paths import RuntimePaths


async def start_server_application(
    *,
    paths: RuntimePaths,
    provider_name: str | None,
    workspace_root: str,
    no_plugins: bool,
) -> Context:
    """Start the server host without constructing an Agent session."""
    ensure_initial_config(paths)
    tree = load_server_tree(paths=paths)
    options = ServerOptions(
        provider_name=provider_name,
        workspace_root=Path(workspace_root).resolve(),
        no_plugins=no_plugins,
    )

    ctx = Context(data_dir=paths.data_dir)
    ctx.set("runtime_paths", paths)
    ctx.set("workspace_root", options.workspace_root)
    ctx.set(
        "session_launch",
        SessionLaunch(
            session_id="server",
            thread_id="server",
            workspace_root=options.workspace_root,
            provider_name=provider_name,
            session_paths=paths.session("server"),
            interactive=False,
            is_subagent=False,
        ),
    )
    ctx.set("plugin_overrides", [])
    ctx.set("plugin_dirs", [])
    ctx.set("no_plugins", no_plugins)
    ctx.set("server_options", options)
    ctx.set("agent_application_factory", create_agent_application)
    return await boot_application(
        ctx=ctx,
        tree=tree,
    )


__all__ = ["ServerOptions", "start_server_application"]
