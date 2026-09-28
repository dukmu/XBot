"""Run a configured terminal client on the CLI's event loop."""

from __future__ import annotations

from typing import Protocol

from xcore import Context

from XBotv2.application.boot import boot_application
from XBotv2.application.tree import load_client_tree
from XBotv2.core.paths import RuntimePaths
from XBotv2.loader import PluginOverlay


class TerminalClient(Protocol):
    """Async foreground client provided by the selected client plugin."""

    async def run(self) -> None: ...

    def request_stop(self, reason: str) -> None: ...


async def run_client_application(*, paths: RuntimePaths, overrides: PluginOverlay) -> None:
    """Boot the selected client profile and own its complete async lifetime.

    The terminal app and every plugin resource share the CLI's event loop and
    thread. ``boot_application`` owns cleanup when startup fails; after a
    successful boot, this host owns the single context-disposal path.
    """
    context = Context(data_dir=paths.data_dir)
    context = await boot_application(
        ctx=context,
        tree=load_client_tree(paths=paths, overrides=overrides),
    )
    try:
        terminal: TerminalClient = context.require("terminal_client")
        await terminal.run()
    finally:
        await context.destroy()


__all__ = ["TerminalClient", "run_client_application"]
