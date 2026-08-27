"""Context builder component: the prompt context builder as an XCore service.

The builder assembles the model-facing context from registered components and
prompt fragments; the engine and the prompts component consume it through
``ctx.context_builder``.
"""

from __future__ import annotations

from typing import Any

from XBotv2.context_builder.builder import ContextBuilder
from XBotv2.context_builder.events import (
    BUILD_CONTEXT,
    CONTEXT_COMPONENTS_BUILT,
    ContextBuildRequest,
    ContextComponentsBuilt,
)
from XBotv2.core.runtime_logging import RuntimeLog


class ContextBuilderComponent:
    """Register the context builder as ``ctx.context_builder``."""

    name = "xbot.context_builder"
    inject = ["runtime_log"]

    def apply(self, ctx: Any, config: Any = None) -> None:
        builder = ContextBuilder()
        ctx.set("context_builder", builder)
        ctx.on(
            BUILD_CONTEXT,
            ContextBuildHandler(builder, ctx, ctx.runtime_log).build,
        )


class ContextBuildHandler:
    def __init__(
        self,
        builder: ContextBuilder,
        events: Any,
        runtime_log: RuntimeLog,
    ) -> None:
        self._builder = builder
        self._events = events
        self._log = runtime_log.bind("context")

    async def build(self, event: ContextBuildRequest) -> None:
        components = self._builder.build_components(
            messages=event.messages,
            agent_name=event.agent_name,
            agent_role=event.agent_role,
            user_name=event.user_name,
            user_id=event.user_id,
            developer_instructions=event.developer_instructions,
            instructions=event.instructions,
            memory=event.memory,
            sandbox_summary=event.sandbox_summary,
            runtime_paths=event.runtime_paths,
            system_notice=event.system_notice,
            turn_count=event.turn_count,
            active_subagents=event.active_subagents,
        )
        component_event = ContextComponentsBuilt(
            components=components,
            session=event.session,
        )
        await self._events.emit(CONTEXT_COMPONENTS_BUILT, component_event)
        event.context_messages = self._builder.messages_from_components(
            component_event.components
        )
        sources: dict[str, int] = {}
        for component in component_event.components:
            sources[component.source] = sources.get(component.source, 0) + 1
        self._log.debug(
            "context.components",
            sources=sources,
            plugin_fragments=sorted(
                component.plugin_name
                for component in component_event.components
                if component.plugin_name
            ),
        )
        self._log.info(
            "context.built",
            session_id=event.session.session_id,
            thread_id=event.session.thread_id,
            history_messages=len(event.messages),
            components=len(component_event.components),
            component_sources=sources,
            output_messages=len(event.context_messages),
            system_chars=(
                len(str(event.context_messages[0].content))
                if event.context_messages
                and event.context_messages[0].role == "system"
                else 0
            ),
        )


plugin = ContextBuilderComponent()
