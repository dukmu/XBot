"""Provider route directory and configured provider definitions (``ctx.llm``).

Aligns with DeepSeek Harness's ``dsh-llm``: the service is a provider
directory — adapter routes register their factories (``ctx.llm.register``)
and application composition creates a client through ``ctx.llm.create(...)``.
The
service also carries the *configured* providers from the ``llm`` plugin's
tree config (``default`` + ``providers``), so runtime code can resolve a
provider by name without reading a separate ``providers.yaml`` document.

Provider definitions are stored raw and parsed on demand: only the selected
provider's API key is required (``provider_config(name)`` raises when its
``api_key_env`` is unset), so mounting never fails because an unrelated
configured provider lacks a key.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from typing import Callable
from XBotv2.llm.contracts import LlmConfig, ModelConfig, ProviderConfig
from XBotv2.core.providers import BaseProvider
from XBotv2.core.provider import ModelRequest
from XBotv2.core.stream import ModelStreamEvent
from XBotv2.llm.contracts import (
    UnknownProviderError,
    LlmServicePort,
    ModelDescription,
    ModelPort,
    ProviderCatalog,
    ProviderDescription,
)

ProviderFactory = Callable[..., BaseProvider]


class LlmService(LlmServicePort):
    """Provider route directory with configured provider definitions."""

    def __init__(self, config: LlmConfig) -> None:
        self._factories: dict[str, ProviderFactory] = {}
        self._config = config

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
        *,
        require_key: bool = True,
    ) -> ProviderConfig:
        """Resolve one configured provider to its validated adapter instance.

        The name ``"default"`` aliases the configured default provider.
        ``require_key=True`` (selection path) resolves ``api_key_env`` and
        raises when the environment variable is unset; ``require_key=False``
        (listing path) leaves the key unresolved.  The returned catalog entry
        carries ``protocol`` / ``default_model`` / ``models``; the concrete
        request settings for one model come from ``resolve(model)``.
        """
        if name == "default":
            name = self._config.default_provider
        provider = self._config.providers.get(name)
        if provider is None:
            available = ", ".join(self.names()) or "(none)"
            raise UnknownProviderError(
                f"Unknown provider config: {name}. "
                f"Configured providers: {available}."
            )
        if provider.api_key is not None or provider.api_key_env is None:
            return provider
        api_key = os.environ.get(provider.api_key_env)
        if api_key is None and require_key:
            raise ValueError(
                f"Environment variable {provider.api_key_env} is not set"
            )
        if api_key is None:
            return provider
        return provider.model_copy(update={"api_key": api_key})

    def create(
        self,
        provider_config: ProviderConfig,
        model_config: "ModelConfig | None" = None,
        *,
        model: str | None = None,
    ) -> BaseProvider:
        """Create a provider client: protocol -> adapter instance -> model.

        ``model_config`` (from ``provider_config.resolve(model)``) supplies
        the selected model's sampling, capacity, and capability settings.
        """
        if model_config is None:
            model_config = provider_config.resolve(model)
        protocol = provider_config.protocol
        factory = self._factories.get(protocol)
        if factory is None:
            raise ValueError(f"Unknown protocol implementation: {protocol!r}")
        return factory(provider_config, model_config)


class ModelService(ModelPort):
    """Mutable binding for the model selected for the active Agent."""

    def __init__(self) -> None:
        self._provider: BaseProvider | None = None

    def replace(self, provider: BaseProvider) -> None:
        self._provider = provider

    @property
    def provider(self) -> BaseProvider:
        if self._provider is None:
            raise RuntimeError("model port is not bound")
        return self._provider

    async def astream(
        self,
        request: ModelRequest,
    ) -> AsyncIterator[ModelStreamEvent]:
        async for chunk in self.provider.astream(request):
            yield chunk


__all__ = ["LlmService", "ModelService"]
