"""Provide Textual as the terminal client in the client plugin profile."""

from __future__ import annotations

from xcore import Context

from XBotv2.application.client import ClientLaunch
from XBotv2.commands import CommandsPort
from XBotv2.tui.commands import register_client_commands
from XBotv2.tui.config import TextualTuiConfig
from XBotv2.tui.terminal import TextualTerminalClient
from XBotv2.tui.transport import SessionBackend


class TextualTuiPlugin:
    name = "xbot.textual_tui"
    Config = TextualTuiConfig
    inject = ["client_api", "client_launch", "commands"]

    def apply(
        self,
        ctx: Context,
        config: TextualTuiConfig,
    ) -> None:
        terminal = TextualTerminalClient(
            backend=ctx.require("client_api"),
            commands=ctx.require("commands"),
            launch=ctx.require("client_launch"),
            config=config,
        )
        register_client_commands(ctx.require("commands"), terminal.app)
        ctx.set("terminal_client", terminal)


plugin = TextualTuiPlugin()

__all__ = ["TextualTuiPlugin", "plugin"]
