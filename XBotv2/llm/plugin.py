"""LLM component: the provider route directory as an XCore service.

Aligns with DeepSeek Harness's ``dsh-llm``: ``ctx.llm`` is a provider
directory (``LlmService``); this plugin registers the built-in provider
adapters (openai-compatible / anthropic / mock), and the agent loop creates
its model client through ``ctx.llm.create(provider_config, media_root=...)``.
"""

from __future__ import annotations

from typing import Any

from XBotv2.llm.service import LlmService


class LlmComponent:
    """Register the provider route directory as ``ctx.llm``."""

    name = "xbot.llm"

    def apply(self, ctx: Any, config: Any = None) -> None:
        from XBotv2.llm.anthropic import create_anthropic_provider
        from XBotv2.llm.mock import create_mock_provider
        from XBotv2.llm.openai import create_openai_provider

        service = LlmService()
        service.register("mock", create_mock_provider)
        for provider in ("openai", "deepseek", "lmstudio-openai"):
            service.register(provider, create_openai_provider)
        service.register("anthropic", create_anthropic_provider)
        service.register("lmstudio", create_anthropic_provider)
        ctx.set("llm", service)


plugin = LlmComponent()
