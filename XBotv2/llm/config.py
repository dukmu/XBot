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
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_ENV = re.compile(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?")


class ModelConfig(BaseModel):
    """Sampling, capacity, and capability settings for one specific model."""

    model_config = ConfigDict(extra="forbid")

    model: str = Field(min_length=1)
    temperature: float | None = None
    max_context_tokens: int = Field(default=32_000, ge=1)
    max_output_tokens: int | None = Field(default=None, ge=1)
    reasoning_effort: str | None = None
    # Adapter-owned thinking mode ("enabled" / "adaptive" / "disabled" / ...).
    # The adapter serializes it to the vendor wire format (Claude / MiniMax
    # ``extra_body.thinking``, OpenAI-compatible ``extra_body``); None omits
    # it and keeps the provider default.
    thinking: str | None = Field(default=None, min_length=1)
    # Vendor-specific request extras (e.g. Anthropic extra_body / OpenAI
    # top-level options) declared per model; adapter-derived parameters are
    # deep-merged underneath these configured values.
    extra_body: dict[str, Any] = Field(default_factory=dict)
    input_modalities: list[Literal["text", "image"]] = Field(
        default_factory=lambda: ["text"]
    )
    mock_responses: list[dict[str, Any]] = Field(default_factory=list)

    @field_validator("input_modalities")
    @classmethod
    def _validate_input_modalities(cls, value):
        if "text" not in value:
            raise ValueError("input_modalities must include text")
        return list(dict.fromkeys(value))

    @property
    def model_mode(self) -> str:
        return self.reasoning_effort or self.thinking or ""


class ProviderConfig(BaseModel):
    """One vendor adapter instance: protocol implementation + endpoint + catalog."""

    model_config = ConfigDict(extra="forbid")

    protocol: str = "openai"
    base_url: str | None = None
    api_key: str | None = None
    default_model: str
    models: list[ModelConfig] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_catalog(self) -> "ProviderConfig":
        if not self.models:
            raise ValueError("models must list at least one model")
        names = {model.model for model in self.models}
        if self.default_model not in names:
            raise ValueError(
                f"default_model {self.default_model!r} is not listed in models: "
                + ", ".join(sorted(names))
            )
        return self

    def resolve(self, model: str | None = None) -> ModelConfig:
        """Resolve the catalog entry for one model (default when omitted).

        Unknown model names fail closed instead of silently reusing another
        model's settings.
        """
        name = model or self.default_model
        for candidate in self.models:
            if candidate.model == name:
                return candidate
        raise ValueError(
            f"Unknown model {name!r} for protocol {self.protocol!r}; "
            "configured models: " + ", ".join(m.model for m in self.models)
        )


def expand_env(value: str) -> str:
    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in os.environ:
            raise ValueError(f"Environment variable {name} is not set")
        return os.environ[name]

    return _ENV.sub(replace, value)


def _expand(value: Any) -> Any:
    if isinstance(value, str):
        return expand_env(value)
    if isinstance(value, dict):
        return {key: _expand(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_expand(item) for item in value]
    return value


def merge_request_extras(
    derived: dict[str, Any],
    configured: dict[str, Any],
) -> dict[str, Any]:
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
    raw: dict[str, Any],
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
