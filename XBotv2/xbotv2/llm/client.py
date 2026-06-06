"""LLM client factory with multi-provider support.

Supports:
- OpenAI (and OpenAI-compatible like DeepSeek)
- Anthropic (and Anthropic-compatible like LM Studio Qwen)
- DeepSeek (using OpenAI protocol)
- LM Studio (using Anthropic or OpenAI protocol)
"""

from __future__ import annotations

import os
import re
import logging
from typing import Any

from langchain_core.language_models import BaseChatModel


logger = logging.getLogger("xbotv2.llm")


def _expand_env(value: str) -> str:
    """Replace ${VAR} or $VAR patterns with environment variable values."""
    pattern = re.compile(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?")

    def replacer(match):
        var_name = match.group(1)
        return os.environ.get(var_name, match.group(0))

    return pattern.sub(replacer, value)


def create_llm(provider_config: Any) -> BaseChatModel:
    """Create an LLM client from provider config.

    Args:
        provider_config: ProviderConfig model or dict with fields:
            provider, model, base_url, api_key, temperature, max_tokens.

    Returns:
        A BaseChatModel instance.

    Supported providers:
    - openai: Uses ChatOpenAI
    - deepseek: Uses ChatOpenAI with DeepSeek base URL
    - anthropic: Uses ChatAnthropic
    - lmstudio: Uses ChatAnthropic with LM Studio base URL (Anthropic protocol)
    - lmstudio-openai: Uses ChatOpenAI with LM Studio base URL (OpenAI protocol)
    """
    if isinstance(provider_config, dict):
        provider = provider_config.get("provider", "openai")
        model = provider_config.get("model", "gpt-4")
        base_url = provider_config.get("base_url")
        api_key = provider_config.get("api_key", "")
        temperature = provider_config.get("temperature", 0.7)
        max_tokens = provider_config.get("max_tokens", 4096)
    else:
        provider = getattr(provider_config, "provider", "openai")
        model = getattr(provider_config, "model", "gpt-4")
        base_url = getattr(provider_config, "base_url", None)
        api_key = getattr(provider_config, "api_key", "")
        temperature = getattr(provider_config, "temperature", 0.7)
        max_tokens = getattr(provider_config, "max_tokens", 4096)

    # Expand env vars in api_key
    api_key = _expand_env(api_key) if api_key else ""

    # Expand env vars in base_url
    if base_url:
        base_url = _expand_env(base_url)

    if provider == "mock":
        responses = (
            provider_config.get("mock_responses", [])
            if isinstance(provider_config, dict)
            else getattr(provider_config, "mock_responses", [])
        )
        return create_mock_llm(responses)

    # OpenAI-compatible providers (OpenAI, DeepSeek, LM Studio w/ OpenAI protocol)
    if provider in ("openai", "deepseek", "lmstudio-openai"):
        from langchain_openai import ChatOpenAI

        if not api_key:
            raise ValueError(
                f"Provider {provider!r} for model {model!r} requires api_key. "
                "Set the configured environment variable or provider.yaml api_key."
            )

        logger.info(
            "creating openai-compatible llm provider=%s model=%s base_url=%s api_key_set=%s",
            provider,
            model,
            base_url,
            bool(api_key),
        )

        kwargs: dict[str, Any] = dict(
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        if base_url:
            kwargs["base_url"] = base_url
        if api_key:
            kwargs["api_key"] = api_key

        return ChatOpenAI(**kwargs)

    # Anthropic-compatible providers (Anthropic, LM Studio w/ Anthropic protocol)
    if provider in ("anthropic", "lmstudio"):
        from langchain_anthropic import ChatAnthropic

        if not api_key:
            raise ValueError(
                f"Provider {provider!r} for model {model!r} requires api_key. "
                "Set the configured environment variable or provider.yaml api_key."
            )

        logger.info(
            "creating anthropic-compatible llm provider=%s model=%s base_url=%s api_key_set=%s",
            provider,
            model,
            base_url,
            bool(api_key),
        )

        kwargs = dict(
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        if base_url:
            kwargs["base_url"] = base_url
        if api_key:
            kwargs["api_key"] = api_key

        return ChatAnthropic(**kwargs)

    raise ValueError(
        f"Unsupported LLM provider {provider!r}. "
        "Use one of: openai, deepseek, lmstudio-openai, anthropic, lmstudio, mock."
    )


# For testing: return a MockLLM
def create_mock_llm(responses: list[dict[str, Any]] | None = None) -> BaseChatModel:
    from xbotv2.llm.mock import MockLLM
    return MockLLM(responses=responses)
