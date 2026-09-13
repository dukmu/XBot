"""Session captioning plugin registration."""

from __future__ import annotations

from xcore import Context

from XBotv2.agentloop.events import Events
from XBotv2.caption.contracts import CaptionConfig
from XBotv2.caption.service import CaptionService
from XBotv2.caption.tools import build_caption_tool


class CaptionPlugin:
    inject = ["model", "loop_state", "session"]
    name = "caption"
    Config = CaptionConfig

    def apply(self, ctx: Context, config: CaptionConfig) -> None:
        service = CaptionService(
            events=ctx,
            model=ctx.model,
            state=ctx.loop_state.metadata,
            session_id=ctx.session.session_id,
            thread_id=ctx.session.thread_id,
            config=config,
        )
        ctx.on(Events.BEFORE_CONTEXT, service._on_before_context)
        if config.allow_access:
            ctx.tools.register(build_caption_tool(service))
        ctx.set("caption", service)


plugin = CaptionPlugin()

__all__ = ["CaptionPlugin", "plugin"]
