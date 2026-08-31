"""Bind LLM commands after their required session services are available."""

from __future__ import annotations

from typing import Any

from XBotv2.llm.commands import build_llm_commands


class LlmCommandsComponent:
    name = "xbot.llm.commands"
    inject = ["llm", "agent_runtime", "commands"]

    def apply(self, ctx: Any, config: Any = None) -> None:
        for command in build_llm_commands(ctx.agent_runtime, ctx.llm):
            ctx.commands.register(command)
