"""Conversation history compaction plugin registration."""

from __future__ import annotations

from xcore import Context

from XBotv2.commands import Command
from XBotv2.agentloop import Events
from XBotv2.compact.contracts import CompactConfig
from XBotv2.compact.service import CompactService
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
        ctx.on(Events.BEFORE_CONTEXT_BUILD, service._on_before_context)
        ctx.on(Events.MODEL_REQUEST_READY, service._on_model_request_ready)
        ctx.on(Events.MODEL_REQUEST_ERROR, service._on_model_request_error)
        ctx.on(Events.MODEL_RESPONSE_OBSERVED, service._on_after_model_response)
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


__all__ = ["CompactPlugin", "plugin"]
