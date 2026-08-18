"""Prompts component: the prompt-fragment registry as an XCore service.

Wraps the context builder's fragment registration behind fiber-scoped
auto-cleanup; capability plugins add prompt fragments through ``ctx.prompts``.
"""

from __future__ import annotations

from typing import Any

from xcore import bound_effect, current_plugin_name


class PromptsService:
    """Plugin-facing prompt-fragment registry (per-plugin namespace)."""

    def __init__(self, context_builder: Any) -> None:
        self._builder = context_builder

    def add(
        self,
        stage: Any,
        text: str,
        *,
        source: str | None = None,
    ) -> None:
        plugin_name = current_plugin_name()
        self._builder.register_fragment(stage, plugin_name, text, source=source)
        bound_effect(
            lambda: self._builder.unregister_fragment(stage, plugin_name),
        )

    def remove(self, stage: Any, plugin_name: str) -> None:
        self._builder.unregister_fragment(stage, plugin_name)


class PromptsComponent:
    inject = ['context_builder']
    """Register the prompt-fragment registry as ``ctx.prompts``."""

    name = "xbot.prompts"

    def apply(self, ctx: Any, config: Any = None) -> None:
        ctx.set("prompts", PromptsService(ctx.context_builder))


plugin = PromptsComponent()
