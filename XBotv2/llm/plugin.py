"""LLM component: provider route directory and tree provider config.

Aligns with DeepSeek Harness's ``dsh-llm``: ``ctx.llm`` is a provider
directory (``LlmService``); this plugin registers the built-in provider
adapters (openai-compatible / anthropic / mock) and loads the configured
provider definitions (``default`` + ``providers``) from its tree config —
there is no separate ``providers.yaml`` document.  The agent loop creates
its model client through ``ctx.llm.create(provider_config, media_root=...)``.
"""

from __future__ import annotations

from typing import Any

from XBotv2.llm.service import LlmService


def build_llm_service(config: dict[str, Any] | None = None) -> LlmService:
    """Create an ``LlmService`` with the built-in adapters and tree config.

    Used by the llm plugin's ``apply`` and by server-root / CLI code that
    needs the provider directory before a session mounts the plugin.
    """
    from XBotv2.llm.anthropic import create_anthropic_provider
    from XBotv2.llm.mock import create_mock_provider
    from XBotv2.llm.openai import create_openai_provider

    config = config or {}
    service = LlmService()
    service.register("mock", create_mock_provider)
    for provider in ("openai", "deepseek", "lmstudio-openai"):
        service.register(provider, create_openai_provider)
    service.register("anthropic", create_anthropic_provider)
    service.register("lmstudio", create_anthropic_provider)
    service.configure(
        config.get("default"),
        config.get("providers"),
    )
    return service


class LlmComponent:
    """Register the provider route directory as ``ctx.llm``."""

    name = "xbot.llm"

    def apply(self, ctx: Any, config: Any = None) -> None:
        ctx.set("llm", build_llm_service(dict(config or {})))


plugin = LlmComponent()
