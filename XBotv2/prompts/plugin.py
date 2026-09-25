"""Prompts component: the typed prompt-component registry as an XCore service.

Wraps context-builder registration behind fiber-scoped ownership cleanup.
"""

from __future__ import annotations

from functools import partial
from pydantic import JsonValue
from xcore import Context, bound_effect, current_plugin_name

from XBotv2.context_builder.contracts import PromptComponent, PromptComponentRegistry
from XBotv2.prompts.contracts import PromptsPort


class PromptsService(PromptsPort):
    """Plugin-facing prompt component registry."""

    def __init__(self, context_builder: PromptComponentRegistry) -> None:
        self._builder = context_builder

    def add(self, component: PromptComponent) -> None:
        """Register one typed component owned by the applying plugin fiber.

        Static components are fiber state: registering outside a plugin
        ``apply`` would produce ownerless data that nothing ever
        releases. Dynamic per-build content belongs to
        ``CONTEXT_COMPONENTS_BUILT`` instead.
        """
        plugin_name = current_plugin_name()
        if bound_effect(partial(self.remove, plugin_name)) is False:
            raise RuntimeError(
                "Prompt components must be registered from within a plugin "
                f"apply() (no owning fiber for source={component.source!r}); contribute "
                "per-build components via CONTEXT_COMPONENTS_BUILT instead"
            )
        self._builder.register_component(plugin_name, component)

    def remove(self, plugin_name: str) -> None:
        self._builder.unregister_owner(plugin_name)


class PromptsComponent:
    inject = ['context_builder']
    """Register the prompt-component registry as ``ctx.prompts``."""

    name = "xbot.prompts"

    def apply(
        self, ctx: Context, config: dict[str, JsonValue] | None = None
    ) -> None:
        ctx.set("prompts", PromptsService(ctx.context_builder))


plugin = PromptsComponent()
