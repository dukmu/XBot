"""Context builder component: the prompt context builder as an XCore service.

The builder assembles the model-facing context from registered components and
prompt fragments; the engine and the prompts component consume it through
``ctx.context_builder``.
"""

from __future__ import annotations

from typing import Any

from XBotv2.context_builder.builder import ContextBuilder
from XBotv2.agentloop import EventContext, Events


class ContextBuilderComponent:
    """Register the context builder as ``ctx.context_builder``."""

    name = "xbot.context_builder"

    def apply(self, ctx: Any, config: Any = None) -> None:
        builder = ContextBuilder()
        ctx.set("context_builder", builder)

        async def build(event: EventContext) -> None:
            if event.context_kwargs is None:
                return
            components = builder.build_components(**event.context_kwargs)
            component_event = EventContext(
                messages=event.messages,
                session=event.session,
                context_components=components,
            )
            await ctx.emit(Events.AFTER_CONTEXT_COMPONENTS_BUILD, component_event)
            if component_event.context_components is not None:
                components = component_event.context_components
            event.context_components = components
            event.context_messages = builder.messages_from_components(components)

        ctx.on(Events.CONTEXT_BUILD, build)


plugin = ContextBuilderComponent()
