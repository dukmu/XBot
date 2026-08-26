"""HTTP server application startup."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from XBotv2.application.boot import boot_application
from XBotv2.application.app import create_agent_application
from XBotv2.application.tree import load_server_tree
from XBotv2.config.seed import ensure_initial_config
from XBotv2.server import ServerOptions


async def start_server_application(
    *,
    paths: Any,
    provider_name: str,
    workspace_root: str,
    no_plugins: bool,
) -> Any:
    """Start the server host without constructing an Agent session."""
    ensure_initial_config(paths)
    tree = load_server_tree(paths=paths)
    options = ServerOptions(
        provider_name=provider_name,
        workspace_root=Path(workspace_root).resolve(),
        no_plugins=no_plugins,
    )

    return await boot_application(
        tree=tree,
        data_dir=paths.data_dir,
        services={
            "runtime_paths": paths,
            "workspace_root": options.workspace_root,
            "server_options": options,
            "agent_application_factory": create_agent_application,
        },
    )


__all__ = ["ServerOptions", "start_server_application"]
