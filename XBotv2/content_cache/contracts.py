"""Content-cache policy and externalization result owned by the plugin."""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field, model_validator

from XBotv2.core.artifacts import ArtifactRef


class ContentCachePolicy(BaseModel):
    """One policy for externalizing oversized text without losing its source."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    threshold_chars: int = Field(default=48_000, ge=1)
    preview_chars: int = Field(default=12_000, ge=0)
    tail_chars: int = Field(default=2_000, ge=0)

    @model_validator(mode="after")
    def _validate_bounds(self) -> "ContentCachePolicy":
        if self.preview_chars > self.threshold_chars:
            raise ValueError("preview_chars cannot exceed threshold_chars")
        if self.tail_chars > self.preview_chars:
            raise ValueError("tail_chars cannot exceed preview_chars")
        return self


@dataclass(frozen=True, slots=True)
class ExternalizedContent:
    """Provider-facing preview plus the complete artifact that backs it."""

    preview: str
    original: ArtifactRef
    original_chars: int


__all__ = ["ContentCachePolicy", "ExternalizedContent"]
