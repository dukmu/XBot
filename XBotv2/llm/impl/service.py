"""Provider route directory and configured provider definitions (``ctx.llm``).

Aligns with DeepSeek Harness's ``dsh-llm``: the service is a provider
directory — adapter routes register their factories (``ctx.llm.register``)
and application composition acquires a client from the directory.
The
service also carries the *configured* providers from the ``llm`` plugin's
tree config (``default`` + ``providers``), so runtime code can resolve a
provider by name without reading a separate ``providers.yaml`` document.

Provider definitions are validated after the configuration loader expands
environment references. Concrete remote adapters validate their credential
when they are created.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import aclosing
from typing import Callable
from XBotv2.llm.api.contracts import (
    LlmConfig,
    ModelConfig,
    ProviderConfig,
    UnknownProviderError,
    LlmServicePort,
    ModelDescription,
    ModelPort,
    ProviderCatalog,
    ProviderDescription,
    ProviderUse,
)
from XBotv2.core.api.providers import Provider
from XBotv2.core.api.provider import ModelRequest
from XBotv2.core.api.stream import ModelStreamEvent

ProviderFactory = Callable[..., Provider]


class LlmService(LlmServicePort):
    """Provider route directory with configured provider definitions."""

    def __init__(self, config: LlmConfig, *, factories: dict[str, ProviderFactory] | None = None) -> None:
        self._factories: dict[str, ProviderFactory] = factories if factories is not None else {}
        self._config = config
        self._clients = {}
        self._closing = False
        self._close_task = None
        self._releasing = set()

    def register(self, provider: str, factory: ProviderFactory) -> None:
        if provider in self._factories:
            raise ValueError(f"Provider {provider!r} is already registered")
        self._factories[provider] = factory

    def unregister(self, provider: str) -> bool:
        return self._factories.pop(provider, None) is not None

    def providers(self) -> tuple[str, ...]:
        """Adapter route names (openai / anthropic / mock / ...)."""
        return tuple(self._factories)

    def has(self, provider: str) -> bool:
        return provider in self._factories

    def default_name(self) -> str:
        """Name of the provider used when no provider is selected."""
        return self._config.default_provider

    def names(self) -> tuple[str, ...]:
        """Configured provider names (minimax / deepseek / ...)."""
        return tuple(self._config.providers)

    def catalog(self) -> ProviderCatalog:
        return ProviderCatalog(
            default=self.default_name(),
            providers=tuple(
                ProviderDescription(
                    name=name,
                    provider=provider.protocol,
                    default_model=provider.default_model,
                    models=tuple(
                        ModelDescription(
                            model=model.model,
                            max_context_tokens=model.max_context_tokens,
                            max_output_tokens=model.max_output_tokens,
                            reasoning_effort=model.reasoning_effort or "",
                            effort=tuple(model.effort or ()),
                            thinking=model.thinking or "",
                            input_modalities=tuple(model.input_modalities),
                        )
                        for model in provider.models
                    ),
                )
                for name, provider in self._config.providers.items()
            ),
        )

    def provider_config(
        self,
        name: str,
    ) -> ProviderConfig:
        """Return one validated provider entry; ``default`` is an alias."""
        if name == "default":
            name = self._config.default_provider
        provider = self._config.providers.get(name)
        if provider is None:
            available = ", ".join(self.names()) or "(none)"
            raise UnknownProviderError(
                f"Unknown provider config: {name}. "
                f"Configured providers: {available}."
            )
        return provider

    def acquire(
        self,
        provider_config: ProviderConfig,
        model_config: ModelConfig | None = None,
    ) -> "ProviderLease":
        if self._closing:
            raise RuntimeError("provider directory is closed")
        model_config = model_config or provider_config.resolve()
        factory = self._factories.get(provider_config.protocol)
        if factory is None:
            raise ValueError(f"Unknown protocol implementation: {provider_config.protocol!r}")
        key = (provider_config.model_dump_json(), model_config.model_dump_json())
        entry = self._clients.get(key)
        if entry is None:
            entry = _Client(factory(provider_config, model_config))
            self._clients[key] = entry
        entry.references += 1
        return ProviderLease(self, key, entry)

    async def _release(self, key, entry):
        entry.references -= 1
        if not entry.references and self._clients.get(key) is entry:
            del self._clients[key]
            task = asyncio.create_task(entry.close())
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
            self._closing = True
            entries = list(self._clients.values())
            releasing = list(self._releasing)
            self._clients.clear()

            async def close_entries():
                async def close_entry(entry):
                    await entry.drained.wait()
                    await entry.close()

                results = await asyncio.gather(*(close_entry(entry) for entry in entries), *releasing, return_exceptions=True)
                for result in results:
                    if isinstance(result, BaseException):
                        raise result

            self._close_task = asyncio.create_task(close_entries())
        try:
            await asyncio.shield(self._close_task)
        except asyncio.CancelledError:
            await self._close_task
            raise


class _Client:
    def __init__(self, provider):
        self.provider = provider
        self.references = 0
        self.streams = 0
        self.drained = asyncio.Event()
        self.drained.set()
        self.close_task = None

    async def close(self):
        if self.close_task is None:
            async def close_provider():
                close = getattr(self.provider, "close", None)
                if close is not None:
                    await close()
            self.close_task = asyncio.create_task(close_provider())
        try:
            await asyncio.shield(self.close_task)
        except asyncio.CancelledError:
            await self.close_task
            raise


class ProviderLease:
    def __init__(self, service, key, entry):
        self._service, self._key, self._entry = service, key, entry
        self._released = False

    async def close(self):
        if not self._released:
            self._released = True
            await self._service._release(self._key, self._entry)

    async def astream(self, request):
        if self._released or self._service._closing:
            raise RuntimeError("provider lease is closed")
        entry = self._entry
        entry.references += 1
        entry.streams += 1
        entry.drained.clear()
        try:
            async with aclosing(entry.provider.astream(request)) as stream:
                async for chunk in stream:
                    yield chunk
        finally:
            entry.streams -= 1
            if not entry.streams:
                entry.drained.set()
            await self._service._release(self._key, entry)


class ModelService(ModelPort):
    """Thread binding; active streams retain the selected client independently."""

    def __init__(self) -> None:
        self._provider = None
        self._use: ProviderUse | None = None
        self._closed = False

    async def replace(self, provider: ProviderUse) -> None:
        if self._closed:
            await provider.close()
            raise RuntimeError("model port is closed")
        previous, self._use = self._use, provider
        self._provider = provider
        if previous is not None:
            await previous.close()

    async def replace_override(self, provider: Provider) -> None:
        if self._closed:
            raise RuntimeError("model port is closed")
        previous, self._use = self._use, None
        self._provider = provider
        if previous is not None:
            await previous.close()

    async def close(self):
        self._closed = True
        previous, self._use = self._use, None
        self._provider = None
        if previous is not None:
            await previous.close()

    async def astream(self, request: ModelRequest) -> AsyncIterator[ModelStreamEvent]:
        provider = self._provider
        if provider is None:
            raise RuntimeError("model port is not bound")
        async with aclosing(provider.astream(request)) as stream:
            async for chunk in stream:
                yield chunk


__all__ = ["LlmService", "ModelService"]
