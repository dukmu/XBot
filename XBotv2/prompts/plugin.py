"""Prompts component: the prompt-fragment registry as an XCore service.

Wraps the context builder's fragment registration behind fiber-scoped
auto-cleanup; capability plugins add prompt fragments through ``ctx.prompts``.
"""

from __future__ import annotations

from functools import partial
from xcore import Context, bound_effect, current_plugin_name

from XBotv2.context_builder.contracts import PromptFragmentRegistry, PromptFragmentStage
from XBotv2.prompts.contracts import PromptsPort


class PromptsService(PromptsPort):
    """Plugin-facing prompt-fragment registry (per-plugin namespace)."""

    def __init__(self, context_builder: PromptFragmentRegistry) -> None:
        self._builder = context_builder

    def add(
        self,
        stage: PromptFragmentStage,
        text: str,
        *,
        source: str | None = None,
    ) -> None:
        plugin_name = current_plugin_name()
        self._builder.register_fragment(stage, plugin_name, text, source=source)
        bound_effect(partial(self.remove, stage, plugin_name))

    def remove(self, stage: PromptFragmentStage, plugin_name: str) -> None:
        self._builder.unregister_fragment(stage, plugin_name)


class PromptsComponent:
    inject = ['context_builder']
    """Register the prompt-fragment registry as ``ctx.prompts``."""

    name = "xbot.prompts"

    def apply(self, ctx: Context, config: object | None = None) -> None:
        ctx.set("prompts", PromptsService(ctx.context_builder))


plugin = PromptsComponent()
