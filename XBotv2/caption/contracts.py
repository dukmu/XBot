"""Public configuration contracts for session captioning."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from XBotv2.core.messages import ConversationMessage


class CaptionConfig(BaseModel):
    """The persisted and resolved configuration for ``caption``."""

    model_config = ConfigDict(extra="forbid")

    # Whether the first user message triggers an independent LLM caption
    # request (the request itself is never stored as conversation). On by
    # default so every session gets a readable title; an explicit model
    # override (a temporary binding) never triggers it.
    auto: bool = True
    # Whether the main agent is granted the caption tool. Exposed means
    # writable; when disabled the title is human-facing only.
    allow_access: bool = True
    max_chars: int = Field(default=60, ge=10, le=200)
    # An output budget a thinking model can spare for one short title after
    # its reasoning; ``caption.service`` still derives a deterministic title
    # from the first user message when the model emits no content.
    output_tokens: int = Field(default=96, ge=8, le=256)


class CaptionRequest(BaseModel):
    """Canonical conversation snapshot and current title for captioning."""

    messages: tuple[ConversationMessage, ...]
    current_title: str
    model_config = ConfigDict(extra="forbid", frozen=True)


class CaptionResult(BaseModel):
    """Validated human-facing session title produced by the caption owner."""

    title: str = Field(min_length=1, max_length=200)
    model_config = ConfigDict(extra="forbid", frozen=True)


class CaptionTitleError(ValueError):
    """A requested title cannot be normalized to a non-empty value."""

    code = "caption_empty_title"


__all__ = [
    "CaptionConfig",
    "CaptionRequest",
    "CaptionResult",
    "CaptionTitleError",
]
