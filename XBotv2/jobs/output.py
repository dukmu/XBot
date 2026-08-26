"""Bounded output storage for job results.

All bulk text produced by a job goes through an OutputStore. Status, list, and
wait responses only ever carry summaries; the model reads full text through an
explicit ``read_*`` tool backed by these stores.
"""

from __future__ import annotations

from XBotv2.jobs.contracts import OutputChunk, OutputStore


class TextOutputStore:
    """In-memory text buffer addressed by character cursor."""

    def __init__(self, text: str = "") -> None:
        self._text = text

    async def write(self, text: str) -> None:
        self._text += text

    async def read(
        self,
        *,
        cursor: int | None = None,
        max_bytes: int = 8000,
    ) -> OutputChunk:
        length = len(self._text)
        start = max(0, min(cursor or 0, length))
        end = min(length, start + max_bytes)
        data = self._text[start:end]
        next_cursor = end if end < length else None
        return OutputChunk(
            data=data,
            next_cursor=next_cursor,
            eof=end >= length,
            truncated=end < length,
        )

    def all(self) -> str:
        return self._text


__all__ = ["TextOutputStore"]
