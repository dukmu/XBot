"""Provide Textual as the terminal client in the client plugin profile."""

from __future__ import annotations
from XBotv2.commands.api.contracts import COMMANDS
from XBotv2.tui.api.service import TERMINAL_CLIENT

from xcore.api.context import Context
from XBotv2.commands.api.contracts import CommandsPort
from XBotv2.tui.impl.commands import register_client_commands
from XBotv2.tui.impl.config import TextualTuiConfig
from XBotv2.tui.impl.terminal import TextualTerminalClient
from XBotv2.tui.impl.transport import SessionBackend


class TextualTuiPlugin:
    name = "xbot.textual_tui"
    Config = TextualTuiConfig
    inject = ["client_api", COMMANDS]

    def apply(
        self,
        ctx: Context,
        config: TextualTuiConfig,
    ) -> None:
        terminal = TextualTerminalClient(
            backend=ctx.require("client_api"),
            commands=ctx.require(COMMANDS),
            config=config,
        )
        register_client_commands(ctx.require(COMMANDS), terminal.app)
        ctx.set(TERMINAL_CLIENT, terminal)


def mount_process(ctx, config):
    TextualTuiPlugin().apply(ctx, config)


mount_process.inject = getattr(TextualTuiPlugin, "inject", ())
mount_process.provide = ('terminal_client',)
mount_process.Config = getattr(TextualTuiPlugin, "Config", None)
