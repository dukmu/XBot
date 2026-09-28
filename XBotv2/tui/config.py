"""Validated settings owned by the Textual client plugin."""

from pydantic import BaseModel, ConfigDict, Field

DEFAULT_HISTORY_WINDOW = 50
DEFAULT_HISTORY_RETENTION = 2000


class TextualTuiConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    render_interval: float = Field(default=0.1, gt=0)
    transcript_limit: int = Field(default=100, gt=0)
    workspace: str = "."
    session_id: str = ""
    thread_id: str = "agent"
    agent: str | None = None
    history_window: int = Field(default=DEFAULT_HISTORY_WINDOW, gt=0)
    history_retention: int = Field(default=DEFAULT_HISTORY_RETENTION, gt=0)


__all__ = ["TextualTuiConfig"]
