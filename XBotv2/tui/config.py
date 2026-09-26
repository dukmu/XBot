"""Validated settings owned by the Textual client plugin."""

from pydantic import BaseModel, ConfigDict, Field


class TextualTuiConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    render_interval: float = Field(default=0.1, gt=0)
    transcript_limit: int = Field(default=100, gt=0)


__all__ = ["TextualTuiConfig"]
