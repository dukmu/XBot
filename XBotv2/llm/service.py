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

from pathlib import Path
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

    def validate_catalog(self, paths: Any, workspace_root: Any) -> dict[str, Any]:
        """Fail-closed validation of the merged provider catalog.

        Owned by the LLM service so a system soft restart can reject an
        invalid catalog before any tree entry is re-applied.  Returns the
        validated merged catalog.
        """
        from XBotv2.core.errors import OperationError
        from XBotv2.llm.config import merged_provider_config, parse_provider_config

        merged = merged_provider_config(paths, Path(workspace_root))
        errors: list[str] = []
        for name, raw in (merged.get("providers") or {}).items():
            try:
                parse_provider_config(dict(raw), require_key=False)
            except Exception as error:  # noqa: BLE001 - report catalog errors
                errors.append(f"{name}: {error}")
        if errors:
            raise OperationError(
                "config_invalid",
                "Provider catalog is invalid: " + "; ".join(errors),
            )
        return merged

    def names(self) -> tuple[str, ...]:
        """Configured provider names (minimax / deepseek / ...)."""
        return tuple(self._providers)

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
        provider_config: ProviderConfig,
        model_config: Any = None,
        *,
        model: str | None = None,
        media_root: str | None = None,
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
        return factory(provider_config, model_config, media_root=media_root)


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
