"""Public configuration contract for current-user content caching."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ContentCacheConfig(BaseModel):
    """The persisted and resolved configuration for ``content_cache``."""

    model_config = ConfigDict(extra="forbid")

    cache_threshold_chars: int = Field(default=48_000, ge=1)
    preview_chars: int = Field(default=12_000, ge=0)
    tail_chars: int = Field(default=2_000, ge=0)

    @model_validator(mode="after")
    def _validate_bounds(self) -> "ContentCacheConfig":
        if self.preview_chars > self.cache_threshold_chars:
            raise ValueError(
                "preview_chars cannot exceed cache_threshold_chars"
            )
        if self.tail_chars > self.preview_chars:
            raise ValueError("tail_chars cannot exceed preview_chars")
        return self


__all__ = ["ContentCacheConfig"]
