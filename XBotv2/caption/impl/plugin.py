"""Session captioning plugin registration."""

from __future__ import annotations
from XBotv2.llm.api.contracts import MODEL

from XBotv2.agentloop.api.contracts import TOOLS
from XBotv2.agentloop.api.state import LOOP_STATE
from xcore.api.context import Context
from XBotv2.agentloop.api.events import Events
from XBotv2.caption.api.models import CaptionConfig
from XBotv2.caption.impl.service import CaptionService
from XBotv2.caption.impl.tools import build_caption_tool


class CaptionPlugin:
    inject = [TOOLS, MODEL, LOOP_STATE, "session", "session_launch", "usage"]
    name = "caption"
    Config = CaptionConfig

    def apply(self, ctx: Context, config: CaptionConfig) -> None:
        is_subagent = ctx.session_launch.is_subagent
        service = CaptionService(
            events=ctx,
            model=ctx.require(MODEL),
            state=ctx.require(LOOP_STATE).metadata,
            usage=ctx.usage,
            session_id=ctx.session.session_id,
            config=config,
            is_subagent=is_subagent,
        )
        # An independent observer: it never short-circuits and must run even
        # when another listener answers BEFORE_CONTEXT first.
        ctx.on(Events.BEFORE_CONTEXT_BUILD, service._on_before_context, prepend=True)
        if config.allow_access and not is_subagent:
            ctx.require(TOOLS).register(build_caption_tool(service))
        ctx.set("caption", service)


plugin = CaptionPlugin()
