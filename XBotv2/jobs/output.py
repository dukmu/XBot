"""Bounded output storage for job results.

All bulk text produced by a job goes through an OutputStore. Status, list, and
wait responses only ever carry summaries; the model reads full text through an
explicit ``read_*`` tool backed by these stores.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class OutputChunk:
    data: str
    next_cursor: int | None = None
    eof: bool = False
    truncated: bool = False


class OutputStore(Protocol):
    """Cursor-based readable output buffer."""

    async def read(
        self,
        *,
        cursor: int | None = None,
        max_bytes: int = 8000,
    ) -> OutputChunk: ...


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


class StreamOutputStore:
    """Growing stdout/stderr buffer with byte-cursor reads."""

    def __init__(self) -> None:
        self._parts: list[bytes] = []

    async def write_bytes(self, data: bytes) -> None:
        if data:
            self._parts.append(data)

    async def read(
        self,
        *,
        cursor: int | None = None,
        max_bytes: int = 8000,
    ) -> OutputChunk:
        full = b"".join(self._parts)
        length = len(full)
        start = max(0, min(cursor or 0, length))
        end = min(length, start + max_bytes)
        data = full[start:end].decode("utf-8", errors="replace")
        next_cursor = end if end < length else None
        return OutputChunk(
            data=data,
            next_cursor=next_cursor,
            eof=end >= length,
            truncated=end < length,
        )

    def snapshot_text(self) -> str:
        return b"".join(self._parts).decode("utf-8", errors="replace")


class CombinedShellOutput:
    """Merged view over separate stdout and stderr stores.

    ``stream="stdout"``, ``"stderr"``, or ``"combined"`` selects the view; the
    combined stream interleaves by store so cursor progress stays monotonic.
    """

    def __init__(self, stdout: StreamOutputStore, stderr: StreamOutputStore) -> None:
        self.stdout = stdout
        self.stderr = stderr

    async def read(
        self,
        *,
        cursor: int | None = None,
        max_bytes: int = 8000,
        stream: str = "combined",
    ) -> OutputChunk:
        store = (
            self.stderr
            if stream == "stderr"
            else self.stdout
            if stream == "stdout"
            else self
        )
        if store is not self:
            return await store.read(cursor=cursor, max_bytes=max_bytes)
        combined = self.stdout.snapshot_text() + self.stderr.snapshot_text()
        return await _read_text(combined, cursor, max_bytes)


async def _read_text(
    text: str,
    cursor: int | None,
    max_bytes: int,
) -> OutputChunk:
    length = len(text)
    start = max(0, min(cursor or 0, length))
    end = min(length, start + max_bytes)
    data = text[start:end]
    return OutputChunk(
        data=data,
        next_cursor=end if end < length else None,
        eof=end >= length,
        truncated=end < length,
    )


__all__ = [
    "CombinedShellOutput",
    "OutputChunk",
    "OutputStore",
    "StreamOutputStore",
    "TextOutputStore",
]
