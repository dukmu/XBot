"""Transport-neutral process Session host composition."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from XBotv2.application.app import create_agent_application
from XBotv2.application.boot import boot_application
from XBotv2.application.tree import load_session_host_tree
from XBotv2.session import SessionHostOptions, SessionHostPort


@dataclass(slots=True)
class MountedSessionHost:
    """Owning handle that exposes only the public Session host service."""

    sessions: SessionHostPort
    _context: Any

    async def close(self) -> None:
        await self._context.destroy()


async def start_session_host_application(
    *,
    paths: Any,
    workspace_root: str | Path,
) -> MountedSessionHost:
    """Mount persistence and Session hosting without an HTTP carrier."""
    tree = load_session_host_tree(paths=paths)
    options = SessionHostOptions(Path(workspace_root).resolve())

    def prepare(ctx: Any) -> None:
        ctx.set("runtime_paths", paths)
        ctx.set("session_host_options", options)
        ctx.set("agent_application_factory", create_agent_application)

    context = await boot_application(
        tree=tree,
        data_dir=paths.data_dir,
        prepare=prepare,
    )
    return MountedSessionHost(context.session_host, context)


__all__ = ["MountedSessionHost", "start_session_host_application"]
