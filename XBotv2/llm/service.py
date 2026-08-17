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

from typing import Any, Callable

from XBotv2.llm.config import ProviderConfig, parse_provider_config
from XBotv2.core.providers import BaseProvider

ProviderFactory = Callable[..., BaseProvider]


class LlmService:
    """Provider route directory with configured provider definitions."""

    def __init__(self) -> None:
        self._factories: dict[str, ProviderFactory] = {}
        self._default = "default"
        self._providers: dict[str, dict[str, Any]] = {}

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

    def configure(
        self,
        default: str | None,
        providers: dict[str, Any] | None,
    ) -> None:
        """Store the configured provider definitions from the tree config."""
        self._default = default or "default"
        self._providers = {
            str(name): dict(raw)
            for name, raw in (providers or {}).items()
        }

    def default_name(self) -> str:
        """Name of the provider used when no provider is selected."""
        return self._default

    def names(self) -> tuple[str, ...]:
        """Configured provider names (minimax / deepseek / ...)."""
        return tuple(self._providers)

    def provider_config(
        self,
        name: str,
        *,
        require_key: bool = True,
    ) -> ProviderConfig:
        """Resolve one configured provider to a validated config.

        The name ``"default"`` aliases the configured default provider.
        ``require_key=True`` (selection path) resolves ``api_key_env`` and
        raises when the environment variable is unset; ``require_key=False``
        (listing path) leaves the key unresolved.
        """
        if name == "default":
            name = self._default
        raw = self._providers.get(name)
        if raw is None:
            available = ", ".join(self.names()) or "(none)"
            raise ValueError(
                f"Unknown provider config: {name}. "
                f"Configured providers: {available}."
            )
        return parse_provider_config(raw, require_key=require_key)

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


class ModelService:
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

    def __getattr__(self, name: str) -> Any:
        return getattr(self.provider, name)


__all__ = ["LlmService", "ModelService"]
