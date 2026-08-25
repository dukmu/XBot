"""LLM component: provider route directory and tree provider config.

Aligns with DeepSeek Harness's ``dsh-llm``: ``ctx.llm`` is a provider
directory (``LlmService``); this plugin registers the built-in provider
adapters (openai-compatible / anthropic / mock) and loads the configured
provider definitions (``default`` + ``providers``) from its tree config —
there is no separate ``providers.yaml`` document. Application composition
creates the model client through ``ctx.llm.create(...)`` and passes the
provider-neutral port to the loop.
"""

from __future__ import annotations

from typing import Any

from XBotv2.llm.service import LlmService, ModelService
from XBotv2.core.operations import EmptyRequest
from XBotv2.llm.contracts import (
    LIST_PROVIDERS,
    ProviderCatalog,
)


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
    service.register("openai", create_openai_provider)
    service.register("anthropic", create_anthropic_provider)
    service.configure(
        config.get("default"),
        config.get("providers"),
    )
    for name in service.names():
        service.provider_config(name, require_key=False)
    return service


class LlmComponent:
    """Register the provider route directory as ``ctx.llm``."""

    name = "xbot.llm"

    def apply(self, ctx: Any, config: Any = None) -> None:
        service = build_llm_service(dict(config or {}))
        ctx.set("llm", service)
        ctx.set("model", ModelService())

        ctx.on(LIST_PROVIDERS.name, ProviderCatalogHandler(service).list_providers)


class ProviderCatalogHandler:
    def __init__(self, service: LlmService) -> None:
        self._service = service

    def list_providers(self, _request: EmptyRequest) -> ProviderCatalog:
        return self._service.catalog()



plugin = LlmComponent()
