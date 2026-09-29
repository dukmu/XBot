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

from pydantic import JsonValue

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
) -> ProviderConfig:
    """Validate one provider entry after configuration expansion."""
    # Environment references were expanded at the config-load boundary
    # (``expand_env_refs`` over the plugin tree); this parser validates only.
    from XBotv2.llm.contracts import ProviderConfig

    return ProviderConfig.model_validate(raw)


__all__ = [
    "merge_request_extras",
    "parse_provider_config",
]
