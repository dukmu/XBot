"""LLM component: provider route directory and tree provider config.

Aligns with DeepSeek Harness's ``dsh-llm``: ``ctx.llm`` is a provider
directory (``LlmService``); this plugin registers the built-in provider
adapters (openai-compatible / anthropic / mock) and loads the configured
provider definitions (``default`` + ``providers``) from its tree config —
there is no separate ``providers.yaml`` document. Application composition
acquires the model client through the provider directory and passes the
provider-neutral port to the loop.
"""

from __future__ import annotations

import asyncio
from XBotv2.commands.api.contracts import COMMANDS

from xcore.api.context import Context
from xcore.api.services import ServiceKey
from XBotv2.agents.api.contracts import AGENT_RUNTIME
from XBotv2.llm.impl.service import LlmService, ModelService
from XBotv2.llm.impl.commands import build_llm_commands
from XBotv2.core.api.operations import EmptyRequest
from XBotv2.llm.api.contracts import LlmConfig, LIST_PROVIDERS, ProviderCatalog, LLM, MODEL
from XBotv2.llm.impl.router import build_router
from XBotv2.server.api.contracts import contribute_router


async def mount_http(ctx: Context) -> None:
    await contribute_router(
        ctx,
        owner="xbot.llm.http",
        router=build_router(events=ctx, sessions=ctx.sessions),
    )


def mount_commands(ctx: Context) -> None:
    for command in build_llm_commands(ctx.require(AGENT_RUNTIME), ctx.require(LLM)):
        ctx.require(COMMANDS).register(command)


def build_llm_service(config: LlmConfig | None = None, *, factories=None) -> LlmService:
    """Create an ``LlmService`` with the built-in adapters and tree config.

    Used by the llm plugin's ``apply`` and by server-root / CLI code that
    needs the provider directory before a session mounts the plugin.
    """
    from XBotv2.llm.impl.anthropic import create_anthropic_provider
    from XBotv2.llm.impl.mock import create_mock_provider
    from XBotv2.llm.impl.openai import create_openai_provider

    if config is None:
        config = LlmConfig()
    service = LlmService(config, factories=factories)
    if factories is None:
        service.register("mock", create_mock_provider)
        service.register("openai", create_openai_provider)
        service.register("anthropic", create_anthropic_provider)
    for name in service.names():
        service.provider_config(name)
    return service


class ProviderDirectories:
    def __init__(self):
        self._catalogs = {}
        self._factories = None
        self._close_task = None
        self._releasing = set()

    def acquire(self, config):
        if self._close_task is not None:
            raise RuntimeError("provider directories are closed")
        key = config.model_dump_json()
        item = self._catalogs.get(key)
        if item is None:
            catalog = build_llm_service(config, factories=self._factories)
            self._factories = catalog._factories
            item = [catalog, 0]
            self._catalogs[key] = item
        item[1] += 1
        return item[0]

    async def release(self, config, service):
        key = config.model_dump_json()
        item = self._catalogs.get(key)
        if item is None or item[0] is not service:
            return
        item[1] -= 1
        if item[1] == 0:
            del self._catalogs[key]
            task = asyncio.create_task(service.close())
            self._releasing.add(task)
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                await task
                raise
            finally:
                self._releasing.discard(task)

    async def close(self):
        if self._close_task is None:
            catalogs = [item[0] for item in self._catalogs.values()]
            self._catalogs.clear()
            releasing = list(self._releasing)

            async def close_catalogs():
                results = await asyncio.gather(*(catalog.close() for catalog in catalogs), *releasing, return_exceptions=True)
                for result in results:
                    if isinstance(result, BaseException):
                        raise result

            self._close_task = asyncio.create_task(close_catalogs())
        try:
            await asyncio.shield(self._close_task)
        except asyncio.CancelledError:
            await self._close_task
            raise


PROVIDER_DIRECTORY = ServiceKey[ProviderDirectories]("llm.directory")


def mount_process(ctx, config):
    directory = ProviderDirectories()
    ctx.dispose(directory.close)
    ctx.provide(PROVIDER_DIRECTORY, directory)
    service = directory.acquire(config)
    ctx.provide(LLM, service)
    ctx.on(LIST_PROVIDERS.name, ProviderCatalogHandler(service).list_providers)
    ctx.inject(["server", "sessions"], mount_http)

mount_process.inject = ("runtime_log",)
mount_process.provide = (LLM, PROVIDER_DIRECTORY)
mount_process.Config = LlmConfig


def mount_thread(ctx, config):
    directory = ctx.require(PROVIDER_DIRECTORY)
    service = directory.acquire(config)
    model = ModelService()

    async def close():
        try:
            await model.close()
        finally:
            await directory.release(config, service)

    ctx.dispose(close)
    ctx.provide(LLM, service)
    ctx.provide(MODEL, model)
    ctx.on(LIST_PROVIDERS.name, ProviderCatalogHandler(service).list_providers)
    ctx.inject([LLM, AGENT_RUNTIME, COMMANDS], mount_commands)

mount_thread.inject = (PROVIDER_DIRECTORY,)
mount_thread.provide = (LLM, MODEL)
mount_thread.Config = LlmConfig


class ProviderCatalogHandler:
    def __init__(self, service: LlmService) -> None:
        self._service = service

    def list_providers(self, _request: EmptyRequest) -> ProviderCatalog:
        return self._service.catalog()
