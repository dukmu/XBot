"""Conversation history compaction plugin registration."""

from __future__ import annotations

from typing import Any

from XBotv2.core import Command, Events
from XBotv2.compact.commands import compact_result_message as _compact_result_message
from XBotv2.compact.config import CONFIG_SCHEMA, parse_compact_config
from XBotv2.compact.history import (
    compact_prefix_end as _compact_prefix_end,
    history_chars as _history_chars,
)
from XBotv2.compact.service import CompactService
from XBotv2.compact.summary import (
    invoke_llm as _invoke_llm,
    limit_summary as _limit_summary,
    model_usage as _model_usage,
    strip_summary_heading as _strip_summary_heading,
    summary_request as _summary_request,
)
from XBotv2.compact.tools import build_compact_tool


class CompactPlugin(CompactService):
    inject = ["tools", "commands", "model"]
    name = "compact"
    Config = CONFIG_SCHEMA

    def apply(self, ctx: Any, config: Any = None) -> None:
        self.bind(ctx, ctx.model, parse_compact_config(config))

        ctx.dispose(self._on_unload)
        ctx.on(Events.BEFORE_CONTEXT, self._on_before_context)
        ctx.on(Events.BEFORE_MODEL_REQUEST, self._on_before_model_request)
        ctx.tools.register(build_compact_tool(self))
        ctx.commands.register(Command(
            name="compact",
            description="Compact conversation history immediately while idle.",
            handler=self._compact_command,
            usage="/compact",
            examples=("/compact",),
        ))


plugin = CompactPlugin()


__all__ = [
    "CompactPlugin",
    "plugin",
    "_compact_prefix_end",
    "_compact_result_message",
    "_history_chars",
    "_invoke_llm",
    "_limit_summary",
    "_model_usage",
    "_strip_summary_heading",
    "_summary_request",
]
