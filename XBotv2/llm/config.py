"""Provider and model configuration owned by the LLM plugin.

A configured provider is a vendor adapter instance.  The chain that
constructs an LLM interface is:

1. ``protocol`` — the protocol implementation (``openai`` / ``anthropic`` /
   ``mock``) that owns the wire format;
2. the adapter instance — ``base_url`` / ``api_key`` for that endpoint;
3. the specific model — one entry of the ``models`` catalog, carrying
   sampling, capacity, reasoning, and modality settings.

``default_model`` names the catalog entry used when no explicit model is
selected.  Unsupported values fail closed at parse/resolve time instead of
being silently sent to a vendor.
"""

from __future__ import annotations

import os
import re
from pydantic import JsonValue

from XBotv2.llm.contracts import ModelConfig, ProviderConfig

_ENV = re.compile(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?")


def expand_env(value: str) -> str:
    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in os.environ:
            raise ValueError(f"Environment variable {name} is not set")
        return os.environ[name]

    return _ENV.sub(replace, value)


def _expand(value: JsonValue) -> JsonValue:
    if isinstance(value, str):
        return expand_env(value)
    if isinstance(value, dict):
        return {key: _expand(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_expand(item) for item in value]
    return value


def merge_request_extras(
    derived: dict[str, JsonValue],
    configured: dict[str, JsonValue],
) -> dict[str, JsonValue]:
    """Deep-merge adapter-derived request extras under configured values.

    Vendor-specific ``extra_body`` standards are declared in the model
    catalog; configured values win over the adapter defaults so a vendor can
    restate or extend fields (e.g. Anthropic ``thinking`` needs
    ``budget_tokens`` on some endpoints).
    """
    merged = dict(derived)
    for key, value in configured.items():
        current = merged.get(key)
        merged[key] = (
            merge_request_extras(current, value)
            if isinstance(current, dict) and isinstance(value, dict)
            else value
        )
    return merged


def parse_provider_config(
    raw: dict[str, JsonValue],
    *,
    require_key: bool = True,
) -> ProviderConfig:
    """Validate one provider catalog entry from the llm plugin tree config.

    ``api_key_env`` is resolved against the environment here; the key itself
    is never stored in configuration.  ``require_key=False`` (listing path)
    leaves the key unresolved.
    """
    values = _expand(dict(raw))
    api_key_env = values.pop("api_key_env", None)
    if api_key_env and not values.get("api_key"):
        env_name = str(api_key_env)
        if require_key and env_name not in os.environ:
            raise ValueError(f"Environment variable {env_name} is not set")
        if env_name in os.environ:
            values["api_key"] = os.environ[env_name]
    return ProviderConfig.model_validate(values)


__all__ = [
    "ModelConfig",
    "ProviderConfig",
    "expand_env",
    "merge_request_extras",
    "parse_provider_config",
]
