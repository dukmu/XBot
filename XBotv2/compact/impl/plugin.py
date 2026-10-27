"""Conversation history compaction plugin registration."""

from __future__ import annotations
from XBotv2.llm.api.contracts import MODEL

from XBotv2.agentloop.api.contracts import TOOLS
from XBotv2.agentloop.api.state import LOOP_STATE
from XBotv2.context_builder.api.contracts import CONTEXT_BUILDER
from xcore.api.context import Context
from XBotv2.commands.api.contracts import COMMANDS, Command
from XBotv2.agentloop.api.events import Events
from XBotv2.compact.api.contracts import CompactConfig
from XBotv2.compact.impl.service import CompactService
from XBotv2.compact.impl.tools import build_compact_tool


class CompactPlugin:
    inject = [TOOLS, COMMANDS, MODEL, LOOP_STATE, "usage", "artifacts", CONTEXT_BUILDER]
    name = "compact"
    Config = CompactConfig

    def apply(self, ctx: Context, config: CompactConfig) -> None:
        service = CompactService(
            events=ctx,
            model=ctx.require(MODEL),
            state=ctx.require(LOOP_STATE),
            usage=ctx.usage,
            config=config,
            artifacts=ctx.artifacts,
            compiler=ctx.require(CONTEXT_BUILDER),
        )

        ctx.dispose(service._dispose)
        ctx.on(Events.BEFORE_CONTEXT_BUILD, service._on_before_context)
        ctx.on(Events.MODEL_REQUEST_READY, service._on_model_request_ready)
        ctx.on(Events.MODEL_REQUEST_ERROR, service._on_model_request_error)
        ctx.on(Events.MODEL_RESPONSE_OBSERVED, service._on_after_model_response)
        ctx.on(Events.TURN_END, service._on_turn_end)
        ctx.require(TOOLS).register(build_compact_tool(service))
        ctx.require(COMMANDS).register(Command(
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
