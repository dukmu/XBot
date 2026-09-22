"""Conversation history compaction plugin registration."""

from __future__ import annotations

from xcore import Context

from XBotv2.commands import Command
from XBotv2.agentloop import Events
from XBotv2.compact.commands import compact_result_message as _compact_result_message
from XBotv2.compact.contracts import CompactConfig
from XBotv2.compact.history import (
    compact_prefix_end as _compact_prefix_end,
    history_chars as _history_chars,
)
from XBotv2.compact.service import CompactService
from XBotv2.compact.summary import (
    limit_summary as _limit_summary,
    model_usage as _model_usage,
    strip_summary_heading as _strip_summary_heading,
    summary_request as _summary_request,
)
from XBotv2.compact.tools import build_compact_tool


class CompactPlugin:
    inject = ["tools", "commands", "model", "loop_state", "usage"]
    name = "compact"
    Config = CompactConfig

    def apply(self, ctx: Context, config: CompactConfig) -> None:
        service = CompactService(
            events=ctx,
            model=ctx.model,
            state=ctx.loop_state,
            usage=ctx.usage,
            config=config,
        )

        ctx.dispose(service._dispose)
        ctx.on(Events.BEFORE_CONTEXT, service._on_before_context)
        ctx.on(Events.BEFORE_MODEL_REQUEST, service._on_before_model_request)
        ctx.on(Events.MODEL_REQUEST_ERROR, service._on_model_request_error)
        ctx.on(Events.AFTER_MODEL_RESPONSE, service._on_after_model_response)
        ctx.on(Events.TURN_END, service._on_turn_end)
        ctx.tools.register(build_compact_tool(service))
        ctx.commands.register(Command(
            name="compact",
            effects=("history", "thread"),
            description="Compact conversation history immediately while idle.",
            handler=service._compact_command,
            usage="/compact",
            examples=("/compact",),
        ))
        ctx.set("compact", service)


plugin = CompactPlugin()


__all__ = [
    "CompactPlugin",
    "plugin",
    "_compact_prefix_end",
    "_compact_result_message",
    "_history_chars",
    "_limit_summary",
    "_model_usage",
    "_strip_summary_heading",
    "_summary_request",
]
