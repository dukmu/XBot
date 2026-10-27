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
from XBotv2.commands.api.contracts import COMMANDS

from xcore.api.context import Context
from XBotv2.agents.api.contracts import AGENT_RUNTIME
from XBotv2.llm.impl.service import LlmService, ModelService
from XBotv2.llm.impl.commands import build_llm_commands
from XBotv2.core.api.operations import EmptyRequest
from XBotv2.llm.api.contracts import LlmConfig, LIST_PROVIDERS, ProviderCatalog, LLM, MODEL
from XBotv2.llm.impl.router import build_router
from XBotv2.server import contribute_router


async def mount_http(ctx: Context) -> None:
    await contribute_router(
        ctx,
        owner="xbot.llm.http",
        router=build_router(events=ctx, sessions=ctx.sessions),
    )


def mount_commands(ctx: Context) -> None:
    for command in build_llm_commands(ctx.require(AGENT_RUNTIME), ctx.require(LLM)):
        ctx.require(COMMANDS).register(command)


def build_llm_service(config: LlmConfig | None = None) -> LlmService:
    """Create an ``LlmService`` with the built-in adapters and tree config.

    Used by the llm plugin's ``apply`` and by server-root / CLI code that
    needs the provider directory before a session mounts the plugin.
    """
    from XBotv2.llm.impl.anthropic import create_anthropic_provider
    from XBotv2.llm.impl.mock import create_mock_provider
    from XBotv2.llm.impl.openai import create_openai_provider

    if config is None:
        config = LlmConfig()
    service = LlmService(config)
    service.register("mock", create_mock_provider)
    service.register("openai", create_openai_provider)
    service.register("anthropic", create_anthropic_provider)
    for name in service.names():
        service.provider_config(name)
    return service


class LlmComponent:
    """Register the provider route directory as ``ctx.llm``."""

    name = "xbot.llm"
    inject = ["runtime_log"]
    Config = LlmConfig

    def apply(
        self,
        ctx: Context,
        config: LlmConfig,
    ) -> None:
        service = build_llm_service(config)
        ctx.runtime_log.bind("llm").info(
            "provider.catalog.loaded",
            providers=list(service.names()),
            configured_default=config.default_provider,
        )
        ctx.set(LLM, service)
        ctx.set(MODEL, ModelService())

        ctx.on(LIST_PROVIDERS.name, ProviderCatalogHandler(service).list_providers)
        ctx.inject([LLM, AGENT_RUNTIME, COMMANDS], mount_commands)
        ctx.inject(["server", "sessions"], mount_http)


class ProviderCatalogHandler:
    def __init__(self, service: LlmService) -> None:
        self._service = service

    def list_providers(self, _request: EmptyRequest) -> ProviderCatalog:
        return self._service.catalog()


plugin = LlmComponent()
