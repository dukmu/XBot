"""Provider route directory (the ``ctx.llm`` service contract).

Aligns with DeepSeek Harness's ``dsh-llm``: the service is a provider
directory; provider adapters register their routes (``ctx.llm.register``),
and the agent loop creates a client through ``ctx.llm.create(...)``.  The
llm plugin registers the built-in adapters (openai-compatible / anthropic /
mock); further providers register themselves the same way.
"""

from __future__ import annotations

from typing import Any, Callable

from XBotv2.core.providers import BaseProvider

ProviderFactory = Callable[..., BaseProvider]


class LlmService:
    """Provider route directory with per-name factories."""

    def __init__(self) -> None:
        self._factories: dict[str, ProviderFactory] = {}

    def register(self, provider: str, factory: ProviderFactory) -> None:
        if provider in self._factories:
            raise ValueError(f"Provider {provider!r} is already registered")
        self._factories[provider] = factory

    def unregister(self, provider: str) -> bool:
        return self._factories.pop(provider, None) is not None

    def providers(self) -> tuple[str, ...]:
        return tuple(self._factories)

    def has(self, provider: str) -> bool:
        return provider in self._factories

    def create(
        self,
        provider_config: Any,
        *,
        media_root: str | None = None,
    ) -> BaseProvider:
        """Create a provider client for one provider config."""
        provider = provider_config.provider
        factory = self._factories.get(provider)
        if factory is None:
            raise ValueError(f"Unknown provider: {provider!r}")
        return factory(provider_config, media_root=media_root)


__all__ = ["LlmService"]
