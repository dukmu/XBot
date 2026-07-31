"""Provider factory."""

from __future__ import annotations

import logging
import os
from typing import Any

from xbotv2.config.loader import expand_env
from xbotv2.llm.anthropic import AnthropicProvider
from xbotv2.llm.base import BaseProvider
from xbotv2.llm.openai import OpenAICompatibleProvider

logger = logging.getLogger("xbotv2.llm")


def create_llm(
    provider_config: Any,
    *,
    media_root: str | None = None,
) -> BaseProvider:
    provider = _get_cfg(provider_config, "provider", "openai")
    model = _get_cfg(provider_config, "model", "gpt-4")
    base_url = _get_cfg(provider_config, "base_url")
    api_key = _get_cfg(provider_config, "api_key", "")
    temperature = _get_cfg(provider_config, "temperature", 0.7)
    max_output_tokens = _get_cfg(provider_config, "max_output_tokens")
    reasoning_effort = _get_cfg(provider_config, "reasoning_effort")
    thinking_enabled = _get_cfg(
        provider_config,
        "thinking_enabled",
        False,
    )
    input_modalities = _get_cfg(provider_config, "input_modalities", ["text"])
    max_retries, retry_backoff_factor = _retry_settings()
    api_key = expand_env(api_key) if api_key else ""
    base_url = expand_env(base_url) if base_url else None

    if provider == "mock":
        from xbotv2.llm.mock import MockLLM

        return MockLLM(
            responses=_get_cfg(provider_config, "mock_responses", []),
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
        if max_output_tokens is None:
            raise ValueError("Anthropic providers require max_output_tokens")
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


def _get_cfg(
    provider_config: Any,
    key: str,
    default: Any = None,
) -> Any:
    if isinstance(provider_config, dict):
        return provider_config.get(key, default)
    return getattr(provider_config, key, default)


def _require_api_key(provider: str, model: str, api_key: str) -> None:
    if not api_key:
        raise ValueError(
            f"Provider {provider!r} for model {model!r} requires api_key. "
            "Set the configured environment variable or providers.yaml api_key."
        )


def _retry_settings() -> tuple[int | None, float]:
    retries = os.environ.get("XBOT_PROVIDER_MAX_RETRIES", "").strip().lower()
    max_retries = None if retries in {"", "none", "infinite"} else int(retries)
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


__all__ = ["create_llm"]
