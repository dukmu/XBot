"""Conversation history ownership and mutation contract."""

from __future__ import annotations

import base64
import binascii
import json
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Generic, Literal, Protocol, TypeAlias, TypeVar, overload
from uuid import uuid4

from XBotv2.core.domain import Cursor, HistoryRevision, TransactionEnded, TransactionStarted
from XBotv2.core.messages import ConversationMessage, HumanInputMessage
from pydantic import BaseModel, ConfigDict, Field


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

    def append(self, messages: Sequence[ConversationMessage]) -> tuple[ConversationMessage, ...]: ...

    def replace_surface(
        self,
        source_ids: Sequence[str],
        messages: Sequence[ConversationMessage],
        *,
        operation: str,
        preserve_transcript: bool,
    ) -> tuple[ConversationMessage, ...]: ...

    def record(self, event: DurableEvent, *, durable: bool = False) -> None: ...

    def open_transactions(
        self,
        transaction_kind: str,
    ) -> frozenset[str]: ...


class ConversationHistory(Sequence[ConversationMessage]):
    """The sole owner of the current, effective conversation history."""

    def __init__(
        self,
        messages: Iterable[ConversationMessage] = (),
        *,
        sink: HistorySink | None = None,
    ) -> None:
        self._surface = list(messages)
        self._seen_ids: set[str] = set()
        self._claim_identities(self._surface)
        self._transcript = list(self._surface)
        self._lineage = {message.id: (message.id,) for message in self._surface}
        self._sink = sink
        self._surface_revision = uuid4().hex
        self._transcript_revision = uuid4().hex

    def snapshot(self) -> tuple[ConversationMessage, ...]:
        """The current surface: the conversation as it stands, identities in it."""
        return tuple(self._surface)

    def ids(self) -> tuple[str, ...]:
        """The identity of every message on the current surface, in order."""
        return tuple(message.id for message in self._surface)

    @property
    def surface_revision(self) -> HistoryRevision:
        return HistoryRevision(self._surface_revision)

    @property
    def transcript_revision(self) -> HistoryRevision:
        return HistoryRevision(self._transcript_revision)

    def append(self, message: ConversationMessage) -> None:
        self.extend((message,))

    def extend(self, messages: Iterable[ConversationMessage]) -> None:
        added = tuple(messages)
        if not added:
            return
        stored = self._sink.append(added) if self._sink is not None else added
        self._claim_identities(stored)
        self._surface.extend(stored)
        self._transcript.extend(stored)
        self._lineage.update({message.id: (message.id,) for message in stored})

    def replace(
        self,
        messages: Iterable[ConversationMessage],
        *,
        operation: str = "replace",
    ) -> None:
        replacement = tuple(messages)
        if not self._surface:
            self.extend(replacement)
            return
        self.replace_range(0, len(self._surface), replacement, operation=operation)

    def replace_range(
        self,
        start: int,
        end: int,
        messages: Iterable[ConversationMessage],
        *,
        operation: str,
        preserve_transcript: bool = False,
    ) -> None:
        """Replace one current contiguous surface span by appending an operation."""
        if start < 0 or end > len(self._surface) or start >= end:
            raise ValueError("History replacement range must be non-empty and current")
        replacement = tuple(messages)
        if preserve_transcript and len(replacement) != 1:
            raise ValueError(
                "Transcript-preserving replacement must produce one surface record"
            )
        source = self._surface[start:end]
        origins = tuple(
            origin
            for message in source
            for origin in self._lineage.get(message.id, (message.id,))
        )
        transcript = list(self._transcript)
        transcript_start = None
        if not preserve_transcript:
            transcript_start = self._transcript_span(transcript, origins)
        stored = (
            self._sink.replace_surface(
                tuple(message.id for message in source),
                replacement,
                operation=operation,
                preserve_transcript=preserve_transcript,
            )
            if self._sink is not None
            else replacement
        )
        self._claim_identities(stored)
        if preserve_transcript:
            self._lineage[stored[0].id] = origins
        else:
            assert transcript_start is not None
            transcript[transcript_start:transcript_start + len(origins)] = stored
            self._lineage.update({message.id: (message.id,) for message in stored})
            self._transcript = transcript
            self._transcript_revision = uuid4().hex
        self._surface[start:end] = stored
        self._surface_revision = uuid4().hex

    @staticmethod
    def _transcript_span(
        transcript: list[ConversationMessage],
        source_ids: Sequence[str],
    ) -> int:
        try:
            start = next(
                index
                for index, message in enumerate(transcript)
                if message.id == source_ids[0]
            )
        except StopIteration as exc:
            raise RuntimeError("History transcript sources are not current") from exc
        current = [message.id for message in transcript[start:start + len(source_ids)]]
        if current != list(source_ids):
            raise RuntimeError("History transcript sources are not current")
        return start

    def record(self, event: DurableEvent, *, durable: bool = False) -> None:
        """Append a log-only trajectory event without changing the surface."""
        if self._sink is not None:
            self._sink.record(event, durable=durable)

    def open_transactions(
        self,
        transaction_kind: str,
    ) -> frozenset[str]:
        """Return durable starts that do not yet have a correlated end."""
        if self._sink is None:
            return frozenset()
        return self._sink.open_transactions(transaction_kind)

    def _claim_identities(self, messages: Sequence[ConversationMessage]) -> None:
        identities = [message.id for message in messages]
        if any(not identity for identity in identities):
            raise ValueError("Conversation messages must have identities")
        duplicates = self._seen_ids.intersection(identities)
        if len(identities) != len(set(identities)) or duplicates:
            raise ValueError("Conversation message identities must be unique")
        self._seen_ids.update(identities)

    def page(
        self,
        *,
        limit: int,
        cursor: Cursor | None = None,
    ) -> HistoryPage[ConversationMessage]:
        return page_messages(
            self._surface,
            revision=HistoryRevision(self._surface_revision),
            limit=limit,
            cursor=cursor,
            out_of_range="History cursor is outside the current history",
        )

    def page_transcript(
        self,
        *,
        limit: int,
        cursor: Cursor | None = None,
    ) -> HistoryPage[ConversationMessage]:
        return page_messages(
            self._transcript,
            revision=HistoryRevision(self._transcript_revision),
            limit=limit,
            cursor=cursor,
            out_of_range="History cursor is outside the transcript",
        )

    def replace_last(self, message: ConversationMessage) -> None:
        if not self._surface:
            raise IndexError("Cannot replace the last message of empty history")
        self.replace_range(
            len(self._surface) - 1,
            len(self._surface),
            (message,),
            operation="replace_last",
        )

    def undo(self, turns: int) -> tuple[ConversationMessage, ...]:
        if turns < 1:
            raise ValueError("Undo turns must be positive")
        user_indexes = [
            index
            for index, message in enumerate(self._surface)
            if isinstance(message, HumanInputMessage)
        ]
        if turns > len(user_indexes):
            raise ValueError(
                f"Cannot undo {turns} turns; history has {len(user_indexes)}."
            )
        self.replace_range(
            user_indexes[-turns],
            len(self._surface),
            (),
            operation="undo",
        )
        return self.snapshot()

    def clear(self) -> None:
        if self._surface:
            self.replace_range(0, len(self._surface), (), operation="clear")

    @overload
    def __getitem__(self, index: int) -> ConversationMessage: ...

    @overload
    def __getitem__(self, index: slice) -> list[ConversationMessage]: ...

    def __getitem__(self, index: int | slice) -> ConversationMessage | list[ConversationMessage]:
        return self._surface[index]

    def __iter__(self) -> Iterator[ConversationMessage]:
        return iter(self._surface)

    def __len__(self) -> int:
        return len(self._surface)

    def __eq__(self, other: object) -> bool:
        if isinstance(other, ConversationHistory):
            return self._surface == other._surface
        if isinstance(other, Sequence):
            return self._surface == list(other)
        return NotImplemented

    def __repr__(self) -> str:
        return repr(self._surface)


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
        value = json.loads(base64.b64decode(
            cursor + padding,
            altchars=b"-_",
            validate=True,
        ))
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
        raise HistoryCursorInvalid(
            "History cursor does not match the current history"
        )
    return value[2]


__all__ = [
    "ConversationHistory",
    "HistoryPage",
    "HistoryReader",
    "HistoryCursorInvalid",
    "HistorySink",
    "DurableEventRecorded",
    "TrajectoryEntry",
    "MessageAppended",
    "TrajectoryRead",
    "SurfaceReplaced",
    "DurableEvent",
    "decode_history_cursor",
    "encode_history_cursor",
    "page_messages",
]
