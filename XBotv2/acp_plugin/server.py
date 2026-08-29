"""ACP stdio server entry point."""

from __future__ import annotations

from acp import run_agent

from XBotv2.application.acp import start_acp_application
from XBotv2.core.paths import RuntimePaths


async def run_acp(
    *,
    data_dir: str,
    provider_name: str,
    no_plugins: bool = False,
    selected_agent: str | None = None,
) -> None:
    """Run the XBot ACP adapter over stdin/stdout."""
    context = await start_acp_application(
        paths=RuntimePaths.from_data_dir(data_dir),
        provider_name=provider_name,
        no_plugins=no_plugins,
        selected_agent=selected_agent,
    )
    try:
        await run_agent(context.acp_agent, use_unstable_protocol=True)
    finally:
        await context.destroy()


__all__ = ["run_acp"]
