"""Provider factory and shared provider construction helpers.

``create_llm`` is the module-level provider route (used where no XCore
context is at hand, e.g. the agent-loop's dynamic provider switch); the
plugin-facing route directory is ``XBotv2.llm.service.LlmService``
(``ctx.llm``), which registers the same factories per provider name.
"""

from __future__ import annotations

import logging
import os

from XBotv2.llm.config import ProviderConfig, expand_env
from XBotv2.core.providers import BaseProvider

logger = logging.getLogger("llm")

DEFAULT_PROVIDER_MAX_RETRIES = 16


def create_llm(
    provider_config: ProviderConfig,
    model_config,
    *,
    media_root: str | None = None,
) -> BaseProvider:
    """Create a provider adapter for one adapter instance + specific model.

    Module-level route mirroring ``LlmService.create`` for code without an
    XCore context.
    """
    protocol = provider_config.protocol
    if protocol == "mock":
        from XBotv2.llm.mock import create_mock_provider

        return create_mock_provider(
            provider_config, model_config, media_root=media_root
        )
    if protocol == "openai":
        from XBotv2.llm.openai import create_openai_provider

        return create_openai_provider(
            provider_config, model_config, media_root=media_root
        )
    if protocol == "anthropic":
        from XBotv2.llm.anthropic import create_anthropic_provider

        return create_anthropic_provider(
            provider_config, model_config, media_root=media_root
        )
    raise ValueError(f"Unknown protocol implementation: {protocol!r}")


def _require_api_key(provider: str, model: str, api_key: str) -> None:
    if not api_key:
        raise ValueError(
            f"Provider {provider!r} for model {model!r} requires api_key. "
            "Set the configured environment variable or the llm plugin's "
            "providers.yaml api_key."
        )


def _retry_settings() -> tuple[int | None, float]:
    retries = os.environ.get("XBOT_PROVIDER_MAX_RETRIES", "").strip().lower()
    if not retries:
        max_retries = DEFAULT_PROVIDER_MAX_RETRIES
    elif retries in {"none", "infinite"}:
        max_retries = None
    else:
        max_retries = int(retries)
    backoff = float(
        os.environ.get("XBOT_PROVIDER_RETRY_BACKOFF_FACTOR", "0.5")
    )
    if max_retries is not None and max_retries < 0:
        raise ValueError("XBOT_PROVIDER_MAX_RETRIES must be non-negative")
    if backoff < 0:
        raise ValueError(
            "XBOT_PROVIDER_RETRY_BACKOFF_FACTOR must be non-negative"
        )
    return max_retries, backoff


__all__ = [
    "DEFAULT_PROVIDER_MAX_RETRIES",
    "create_llm",
    "_require_api_key",
    "_retry_settings",
]
