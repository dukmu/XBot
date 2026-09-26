"""Run a configured terminal client on the CLI's event loop."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from xcore import Context

from XBotv2.application.boot import boot_application
from XBotv2.application.tree import load_client_tree
from XBotv2.core.paths import RuntimePaths


@dataclass(frozen=True, slots=True)
class ClientLaunch:
    """Values resolved by the CLI before the client plugin profile starts."""

    data_dir: str
    base_url: str
    uds_path: str | None
    workspace: str
    session_id: str | None
    thread_id: str
    agent: str | None
    history_window: int | None = None
    history_retention: int | None = None


class TerminalClient(Protocol):
    """Async foreground client provided by the selected client plugin."""

    async def run(self) -> None: ...

    def request_stop(self, reason: str) -> None: ...


async def run_client_application(launch: ClientLaunch) -> None:
    """Boot the selected client profile and own its complete async lifetime.

    The terminal app and every plugin resource share the CLI's event loop and
    thread. ``boot_application`` owns cleanup when startup fails; after a
    successful boot, this host owns the single context-disposal path.
    """
    paths = RuntimePaths.from_data_dir(launch.data_dir)
    context = Context(data_dir=paths.data_dir)
    context.set("runtime_paths", paths)
    context.set("client_launch", launch)
    context = await boot_application(
        ctx=context,
        tree=load_client_tree(paths=paths),
    )
    try:
        terminal: TerminalClient = context.require("terminal_client")
        await terminal.run()
    finally:
        await context.destroy()


__all__ = ["ClientLaunch", "TerminalClient", "run_client_application"]
