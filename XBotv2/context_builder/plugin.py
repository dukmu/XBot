"""Context builder component: the prompt context builder as an XCore service.

The builder assembles the model-facing context from registered components and
prompt fragments; the engine and the prompts component consume it through
``ctx.context_builder``.
"""

from __future__ import annotations

from typing import Any

from XBotv2.context_builder.builder import ContextBuilder


class ContextBuilderComponent:
    """Register the context builder as ``ctx.context_builder``."""

    name = "xbot.context_builder"

    def apply(self, ctx: Any, config: Any = None) -> None:
        ctx.set("context_builder", ContextBuilder())


plugin = ContextBuilderComponent()
