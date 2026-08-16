"""Provider factory."""

from __future__ import annotations

import logging
import os

from config.loader import expand_env
from config.models import ProviderConfig
from llm.anthropic import AnthropicProvider
from llm.base import BaseProvider
from llm.openai import OpenAICompatibleProvider

logger = logging.getLogger("llm")

DEFAULT_PROVIDER_MAX_RETRIES = 16


def create_llm(
    provider_config: ProviderConfig,
    *,
    media_root: str | None = None,
) -> BaseProvider:
    provider = provider_config.provider
    model = provider_config.model
    base_url = provider_config.base_url
    api_key = provider_config.api_key or ""
    temperature = provider_config.temperature
    max_output_tokens = provider_config.max_output_tokens
    reasoning_effort = provider_config.reasoning_effort
    thinking_enabled = provider_config.thinking_enabled
    input_modalities = provider_config.input_modalities
    max_retries, retry_backoff_factor = _retry_settings()
    api_key = expand_env(api_key) if api_key else ""
    base_url = expand_env(base_url) if base_url else None

    if provider == "mock":
        from llm.mock import MockLLM

        return MockLLM(
            responses=provider_config.mock_responses,
            input_modalities=input_modalities,
            media_root=media_root,
        )
    if provider in ("openai", "deepseek", "lmstudio-openai"):
        _require_api_key(provider, model, api_key)
        logger.info(
            "creating openai-compatible provider=%s model=%s",
            provider,
            model,
        )
        return OpenAICompatibleProvider(
            model=model,
            api_key=api_key,
            base_url=base_url,
            temperature=temperature,
            max_output_tokens=max_output_tokens,
            reasoning_effort=reasoning_effort,
            thinking_enabled=thinking_enabled,
            max_retries=max_retries,
            retry_backoff_factor=retry_backoff_factor,
            input_modalities=input_modalities,
            media_root=media_root,
        )
    if provider in ("anthropic", "lmstudio"):
        _require_api_key(provider, model, api_key)
        logger.info(
            "creating anthropic provider=%s model=%s",
            provider,
            model,
        )
        return AnthropicProvider(
            model=model,
            api_key=api_key,
            base_url=base_url,
            temperature=temperature,
            max_output_tokens=max_output_tokens,
            reasoning_effort=reasoning_effort,
            thinking_enabled=thinking_enabled,
            max_retries=max_retries,
            retry_backoff_factor=retry_backoff_factor,
            input_modalities=input_modalities,
            media_root=media_root,
        )
    raise ValueError(f"Unknown provider: {provider!r}")


def _require_api_key(provider: str, model: str, api_key: str) -> None:
    if not api_key:
        raise ValueError(
            f"Provider {provider!r} for model {model!r} requires api_key. "
            "Set the configured environment variable or providers.yaml api_key."
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


__all__ = ["DEFAULT_PROVIDER_MAX_RETRIES", "create_llm"]
