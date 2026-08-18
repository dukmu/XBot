"""Provider configuration owned by the LLM plugin."""

from __future__ import annotations

import os
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_ENV = re.compile(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?")


class ProviderConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str = "openai"
    model: str = "gpt-4"
    base_url: str | None = None
    api_key: str | None = None
    temperature: float = 0.7
    max_context_tokens: int = Field(default=32_000, ge=1)
    max_output_tokens: int | None = Field(default=None, ge=1)
    reasoning_effort: str | None = None
    thinking_enabled: bool = False
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

    @model_validator(mode="after")
    def _validate_output_limit(self):
        if self.provider in {"anthropic", "lmstudio"} and self.max_output_tokens is None:
            raise ValueError("Anthropic providers require max_output_tokens")
        return self

    @property
    def model_mode(self) -> str:
        return self.reasoning_effort or (
            "thinking" if self.thinking_enabled else ""
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


def parse_provider_config(
    raw: dict[str, Any],
    *,
    require_key: bool = True,
) -> ProviderConfig:
    values = _expand(dict(raw))
    api_key_env = values.pop("api_key_env", None)
    if api_key_env and not values.get("api_key"):
        env_name = str(api_key_env)
        if require_key and env_name not in os.environ:
            raise ValueError(f"Environment variable {env_name} is not set")
        if env_name in os.environ:
            values["api_key"] = os.environ[env_name]
    return ProviderConfig.model_validate(values)


__all__ = ["ProviderConfig", "expand_env", "parse_provider_config"]
