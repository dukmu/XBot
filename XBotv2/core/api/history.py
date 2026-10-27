"""Conversation history ownership and mutation contract."""

from __future__ import annotations

import base64
import binascii
import json
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Generic, Literal, Protocol, TypeAlias, TypeVar, overload
from XBotv2.core.api.domain import (
    Cursor,
    HistoryRevision,
    TransactionEnded,
    TransactionStarted,
)
from XBotv2.core.api.messages import ConversationMessage
from pydantic import BaseModel, ConfigDict, Field


class HistoryRevisionConflict(ValueError):
    """A mutation was based on an older canonical working set."""


class HistoryCursorInvalid(ValueError):
    """The requested page does not belong to the current history revision."""


T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class HistoryPage(Generic[T]):
    items: tuple[T, ...]
    older_cursor: Cursor | None


class MessageAppended(BaseModel):
    kind: Literal["message_appended"] = "message_appended"
    position: int = Field(ge=1)
    message: ConversationMessage
    model_config = ConfigDict(extra="forbid", frozen=True)


class SurfaceReplaced(BaseModel):
    kind: Literal["surface_replaced"] = "surface_replaced"
    position: int = Field(ge=1)
    operation: str
    transcript_policy: Literal["preserve", "replace"]
    source_ids: tuple[str, ...]
    replacements: tuple[ConversationMessage, ...]
    model_config = ConfigDict(extra="forbid", frozen=True)


DurableEvent: TypeAlias = TransactionStarted | TransactionEnded


class DurableEventRecorded(BaseModel):
    kind: Literal["durable_event_recorded"] = "durable_event_recorded"
    position: int = Field(ge=1)
    timestamp: datetime
    event: DurableEvent
    model_config = ConfigDict(extra="forbid", frozen=True)


TrajectoryEntry: TypeAlias = MessageAppended | SurfaceReplaced | DurableEventRecorded


@dataclass(frozen=True, slots=True)
class TrajectoryRead:
    page: HistoryPage[TrajectoryEntry]
    #: Highest position on the append-only trajectory, so a windowed client can
    #: tell whether it holds the tail and re-anchor without walking cursors.
    newest_position: int


class HistoryReader(Protocol):
    def page(
        self,
        *,
        limit: int,
        cursor: Cursor | None = None,
    ) -> HistoryPage[ConversationMessage]: ...


class HistorySink(Protocol):
    """Append-only trajectory boundary used by one conversation history."""

    async def surface_state(
        self, after_version: int | None = None
    ) -> tuple[int, tuple[ConversationMessage, ...]] | None: ...
    async def begin_turn(self, input_id: str) -> int: ...
    async def page_transcript(
        self, *, limit: int, cursor: Cursor | None = None
    ) -> HistoryPage[ConversationMessage]: ...

    async def append(
        self, messages: Sequence[ConversationMessage], *, expected_version: int
    ) -> int: ...

    async def replace_surface(
        self,
        source_ids: Sequence[str],
        messages: Sequence[ConversationMessage],
        *,
        operation: str,
        preserve_transcript: bool,
        expected_version: int,
    ) -> int: ...

    async def record(self, event: DurableEvent, *, durable: bool = False) -> None: ...

    async def open_transactions(
        self,
        transaction_kind: str,
    ) -> frozenset[str]: ...


class History(Protocol):
    """Mutable conversation surface shared with history-editing capabilities.

    The loop owns its implementation and construction; consumers never own a
    second message list or construct working sets.
    """

    async def synchronize(self) -> None: ...
    async def begin_turn(self, input_id: str) -> int | None: ...

    def snapshot(self) -> tuple[ConversationMessage, ...]: ...

    def ids(self) -> tuple[str, ...]: ...

    @property
    def surface_revision(self) -> HistoryRevision: ...

    @property
    def transcript_revision(self) -> HistoryRevision: ...

    async def append(self, message: ConversationMessage) -> None: ...

    async def extend(self, messages: Iterable[ConversationMessage]) -> None: ...

    async def replace(
        self, messages: Iterable[ConversationMessage], *, operation: str = "replace"
    ) -> None: ...

    async def replace_range(
        self,
        start: int,
        end: int,
        messages: Iterable[ConversationMessage],
        *,
        operation: str,
        preserve_transcript: bool = False,
    ) -> None: ...

    async def record(self, event: DurableEvent, *, durable: bool = False) -> None: ...

    async def open_transactions(self, transaction_kind: str) -> frozenset[str]: ...

    def page(
        self, *, limit: int, cursor: Cursor | None = None
    ) -> HistoryPage[ConversationMessage]: ...

    async def page_transcript(
        self, *, limit: int, cursor: Cursor | None = None
    ) -> HistoryPage[ConversationMessage]: ...

    async def replace_last(self, message: ConversationMessage) -> None: ...

    async def undo(self, turns: int) -> tuple[ConversationMessage, ...]: ...

    async def clear(self) -> None: ...

    @overload
    def __getitem__(self, index: int) -> ConversationMessage: ...

    @overload
    def __getitem__(self, index: slice) -> list[ConversationMessage]: ...

    def __getitem__(
        self, index: int | slice
    ) -> ConversationMessage | list[ConversationMessage]: ...

    def __iter__(self) -> Iterator[ConversationMessage]: ...

    def __len__(self) -> int: ...


def resolve_transcript_sources(
    source_ids: Sequence[str],
    lineage: Mapping[str, tuple[str, ...]],
) -> tuple[str, ...]:
    """Expand validated summary ancestry only when a transcript span changes."""
    pending = list(reversed(source_ids))
    sources = []
    while pending:
        source = pending.pop()
        if source in lineage:
            pending.extend(reversed(lineage[source]))
        else:
            sources.append(source)
    return tuple(sources)


def page_messages(
    messages: Sequence[ConversationMessage],
    *,
    revision: HistoryRevision,
    limit: int,
    cursor: Cursor | None,
    out_of_range: str,
) -> HistoryPage[ConversationMessage]:
    """The newest ``limit`` messages before ``cursor``."""
    if limit < 1:
        raise ValueError("History page limit must be positive")
    if not messages and cursor is not None:
        raise HistoryCursorInvalid(out_of_range)
    end = len(messages) if cursor is None else decode_history_cursor(cursor, revision)
    if end < 0 or end > len(messages):
        raise HistoryCursorInvalid(out_of_range)
    start = max(0, end - limit)
    return HistoryPage(
        items=tuple(messages[start:end]),
        older_cursor=encode_history_cursor(revision, start) if start else None,
    )


def encode_history_cursor(revision: HistoryRevision, offset: int) -> Cursor:
    value = json.dumps([1, revision, offset], separators=(",", ":")).encode()
    return Cursor(base64.urlsafe_b64encode(value).decode().rstrip("="))


def decode_history_cursor(cursor: Cursor, revision: HistoryRevision) -> int:
    try:
        padding = "=" * (-len(cursor) % 4)
        value = json.loads(
            base64.b64decode(
                cursor + padding,
                altchars=b"-_",
                validate=True,
            )
        )
    except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HistoryCursorInvalid("History cursor is invalid") from exc
    if (
        not isinstance(value, list)
        or len(value) != 3
        or value[0] != 1
        or value[1] != revision
        or not isinstance(value[2], int)
        or isinstance(value[2], bool)
    ):
        raise HistoryCursorInvalid("History cursor does not match the current history")
    return value[2]
