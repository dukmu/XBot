"""Prompts component: the prompt-fragment registry as an XCore service.

Wraps the context builder's fragment registration behind fiber-scoped
auto-cleanup; capability plugins add prompt fragments through ``ctx.prompts``.
"""

from __future__ import annotations

from functools import partial
from pydantic import JsonValue
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
        """Register one fragment owned by the applying plugin's fiber.

        Static fragments are fiber state: registering outside a plugin
        ``apply`` would produce an ownerless fragment that nothing ever
        releases. Dynamic per-build content belongs to
        ``CONTEXT_COMPONENTS_BUILT`` instead.
        """
        plugin_name = current_plugin_name()
        if bound_effect(partial(self.remove, stage, plugin_name)) is False:
            raise RuntimeError(
                "Prompt fragments must be registered from within a plugin "
                f"apply() (no owning fiber for source={source!r}); contribute "
                "per-build components via CONTEXT_COMPONENTS_BUILT instead"
            )
        self._builder.register_fragment(stage, plugin_name, text, source=source)

    def remove(self, stage: PromptFragmentStage, plugin_name: str) -> None:
        self._builder.unregister_fragment(stage, plugin_name)


class PromptsComponent:
    inject = ['context_builder']
    """Register the prompt-fragment registry as ``ctx.prompts``."""

    name = "xbot.prompts"

    def apply(
        self, ctx: Context, config: dict[str, JsonValue] | None = None
    ) -> None:
        ctx.set("prompts", PromptsService(ctx.context_builder))


plugin = PromptsComponent()
