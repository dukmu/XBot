"""Run a configured terminal client on the CLI's event loop."""

from __future__ import annotations

from xcore.api.context import Context
import XBotv2.tui.api.service as terminal_api
from XBotv2.application.boot import boot_application
from XBotv2.application.tree import load_client_tree
from XBotv2.core.api.paths import RuntimePaths
from XBotv2.loader import PluginOverlay


async def run_client_application(*, paths: RuntimePaths, overrides: PluginOverlay) -> None:
    """Boot the selected client profile and own its complete async lifetime.

    The terminal app and every plugin resource share the CLI's event loop and
    thread. ``boot_application`` owns cleanup when startup fails; after a
    successful boot, this host owns the single context-disposal path.
    """
    context = Context(data_dir=paths.data_dir)
    _ = context.state
    context = await boot_application(
        ctx=context,
        tree=load_client_tree(paths=paths, overrides=overrides),
    )
    try:
        terminal: terminal_api.TerminalClient = context.require(terminal_api.TERMINAL_CLIENT)
        await terminal.run()
    finally:
        await context.destroy()


__all__ = ["run_client_application"]
