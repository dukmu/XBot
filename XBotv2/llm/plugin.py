"""LLM component: the provider factory as an XCore service (``ctx.llm``).

The engine (core component) creates its model client through this service:
``llm = ctx.llm(provider_config, media_root=...)``.  Provider adapters
(openai-compatible / anthropic / mock) stay inside this plugin package.
"""

from __future__ import annotations

from typing import Any


class LlmComponent:
    """Register the provider factory as ``ctx.llm``."""

    name = "xbot.llm"

    def apply(self, ctx: Any, config: Any = None) -> None:
        from XBotv2.llm.client import create_llm

        ctx.set("llm", create_llm)


plugin = LlmComponent()
