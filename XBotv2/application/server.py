"""HTTP server application startup."""

from __future__ import annotations

from typing import Any

from XBotv2.application.boot import boot_application
from XBotv2.config.tree import load_server_tree


async def start_server_application(
    *,
    paths: Any,
    provider_name: str,
    workspace_root: str,
    no_plugins: bool,
) -> Any:
    """Start the server host without constructing an Agent session."""
    tree = load_server_tree(
        paths=paths,
        provider_name=provider_name,
        workspace_root=workspace_root,
        no_plugins=no_plugins,
    )
    return await boot_application(tree=tree, data_dir=paths.data_dir)


__all__ = ["start_server_application"]
