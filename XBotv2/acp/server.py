"""ACP stdio server entry point."""

from __future__ import annotations

from acp import run_agent

from acp.xbot_agent import XBotACPAgent
from api.paths import RuntimePaths


async def run_acp(
    *,
    data_dir: str,
    provider_name: str,
    no_plugins: bool = False,
    selected_agent: str | None = None,
) -> None:
    """Run the XBot ACP adapter over stdin/stdout."""
    agent = XBotACPAgent(
        paths=RuntimePaths.from_data_dir(data_dir),
        provider_name=provider_name,
        no_plugins=no_plugins,
        selected_agent=selected_agent,
    )
    try:
        await run_agent(agent, use_unstable_protocol=True)
    finally:
        await agent.close()


__all__ = ["run_acp"]
