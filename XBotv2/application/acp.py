"""ACP carrier application startup."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from XBotv2.acp import ACPLaunch
from XBotv2.application.app import create_agent_application
from XBotv2.application.boot import boot_application
from XBotv2.application.tree import load_acp_tree
from XBotv2.config.seed import ensure_initial_config


async def start_acp_application(
    *,
    paths: Any,
    provider_name: str,
    no_plugins: bool,
    selected_agent: str | None,
    llm_override: Any | None = None,
) -> Any:
    """Start the ACP carrier and its shared process-level services."""
    ensure_initial_config(paths)
    tree = load_acp_tree(paths=paths)

    def prepare(ctx: Any) -> None:
        ctx.set("runtime_paths", paths)
        ctx.set("workspace_root", Path(paths.data_dir).resolve())
        ctx.set("agent_application_factory", create_agent_application)
        ctx.set("acp_launch", ACPLaunch(
            provider_name=provider_name,
            no_plugins=no_plugins,
            selected_agent=selected_agent,
            llm_override=llm_override,
        ))

    return await boot_application(
        tree=tree,
        data_dir=paths.data_dir,
        prepare=prepare,
    )


__all__ = ["start_acp_application"]
