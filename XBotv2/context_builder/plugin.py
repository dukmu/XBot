"""Context builder component: the typed context compiler as an XCore service.

The builder assembles model-facing context from typed components; the loop and
the prompts component consume it through
``ctx.context_builder``.
"""

from __future__ import annotations

from pydantic import JsonValue
from xcore import Context

from XBotv2.agentloop import EventPort
from XBotv2.context_builder.builder import ContextBuilder
from XBotv2.context_builder.contracts import FilePromptComponent, InlinePromptComponent
from XBotv2.context_builder.events import (
    BUILD_CONTEXT,
    CONTEXT_COMPONENTS_BUILT,
    ContextBuildRequest,
)
from XBotv2.core.runtime_logging import RuntimeLog
from XBotv2.core.provider import ProviderMessage


class ContextBuilderComponent:
    """Register the context builder as ``ctx.context_builder``."""

    name = "xbot.context_builder"
    inject = ["runtime_log", "artifacts"]

    def apply(
        self, ctx: Context, config: dict[str, JsonValue] | None = None
    ) -> None:
        builder = ContextBuilder(ctx.artifacts)
        ctx.set("context_builder", builder)
        ctx.on(
            BUILD_CONTEXT,
            ContextBuildHandler(builder, ctx, ctx.runtime_log).build,
        )


class ContextBuildHandler:
    def __init__(
        self,
        builder: ContextBuilder,
        events: EventPort,
        runtime_log: RuntimeLog,
    ) -> None:
        self._builder = builder
        self._events = events
        self._log = runtime_log.bind("context")

    async def build(
        self, event: ContextBuildRequest
    ) -> tuple[ProviderMessage, ...]:
        built_context = self._builder.build_components(
            history=event.history,
            runtime_selection=event.runtime_selection,
            user_identity=event.user_identity,
            memory=event.memory,
            sandbox_summary=event.sandbox_summary,
            runtime_paths=event.runtime_paths,
            turn=event.turn,
        )
        await self._events.emit(CONTEXT_COMPONENTS_BUILT, built_context)
        messages = self._builder.messages_from_components(
            built_context,
            artifacts=self._builder.artifacts,
        )
        sources: dict[str, int] = {}
        for component in built_context.components:
            source = (
                component.source
                if isinstance(component, (InlinePromptComponent, FilePromptComponent))
                else "history"
            )
            sources[source] = sources.get(source, 0) + 1
        self._log.debug(
            "context.components",
            sources=sources,
            plugin_components=sorted(
                component.source
                for component in built_context.components
                if isinstance(component, (InlinePromptComponent, FilePromptComponent))
                and component.source not in {
                    "core_instructions",
                    "runtime_environment",
                    "agent_identity",
                    "agent_instructions",
                    "memory",
                }
            ),
        )
        self._log.info(
            "context.built",
            turn=event.turn,
            history_messages=len(event.history),
            components=len(built_context.components),
            component_sources=sources,
            output_messages=len(messages),
            system_chars=(
                sum(len(part.text) for part in messages[0].parts)
                if messages
                and getattr(messages[0], "role", "") == "system"
                else 0
            ),
        )
        return tuple(messages)


plugin = ContextBuilderComponent()
