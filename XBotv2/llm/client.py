"""Shared provider construction helpers."""

from __future__ import annotations

import logging
import json
import os
from pydantic import JsonValue

from XBotv2.llm.contracts import ModelConfig, ProviderConfig

logger = logging.getLogger("xbotv2.llm")

DEFAULT_PROVIDER_MAX_RETRIES = 16


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


def _provider_arguments(
    provider_config: ProviderConfig,
    model_config: ModelConfig,
) -> dict[str, JsonValue]:
    """Resolve configuration shared by concrete remote adapters."""
    api_key = provider_config.api_key or ""
    _require_api_key(provider_config.protocol, model_config.model, api_key)
    max_retries, retry_backoff_factor = _retry_settings()
    return {
        "api_key": api_key,
        "base_url": provider_config.base_url,
        "extra_body": model_config.extra_body,
        "max_retries": max_retries,
        "retry_backoff_factor": retry_backoff_factor,
        "input_modalities": model_config.input_modalities,
    }


class ToolArgumentsError(ValueError):
    """A provider delivered tool-call arguments that are not a JSON object.

    Raised at the provider boundary so a malformed call surfaces as a loud
    error instead of executing the tool with fabricated empty arguments.
    """

    code = "invalid_tool_arguments"


def _parse_tool_args(
    raw: str,
    *,
    tool_name: str = "",
) -> dict[str, JsonValue]:
    """Parse provider tool-call arguments, failing loudly when malformed."""
    if not raw:
        return {}
    label = tool_name or "tool"
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, TypeError) as exc:
        raise ToolArgumentsError(
            f"Malformed tool-call arguments for {label}: {exc}"
        ) from exc
    if not isinstance(value, dict):
        raise ToolArgumentsError(
            f"Tool-call arguments for {label} must be a JSON object, "
            f"got {type(value).__name__}"
        )
    return value


__all__ = [
    "DEFAULT_PROVIDER_MAX_RETRIES",
    "_require_api_key",
    "_retry_settings",
    "_provider_arguments",
    "ToolArgumentsError",
    "_parse_tool_args",
]
