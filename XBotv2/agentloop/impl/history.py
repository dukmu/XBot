"""Effective conversation working set and transcript lineage."""
from __future__ import annotations
from collections.abc import Iterable, Iterator, Sequence
from typing import overload
from uuid import uuid4
from XBotv2.core.api.domain import Cursor, HistoryRevision
from XBotv2.core.api.messages import ConversationMessage, HumanInputMessage
from XBotv2.core.api.history import (
    HistorySink,
    HistoryPage,
    DurableEvent,
    page_messages,
    resolve_transcript_sources,
)

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
        self._seen_ids.update(self._validate_identities(self._surface))
        self._transcript = list(self._surface)
        self._lineage: dict[str, tuple[str, ...]] = {}
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
        identities = self._validate_identities(added)
        if self._sink is not None:
            self._sink.append(added)
        self._seen_ids.update(identities)
        self._surface.extend(added)
        self._transcript.extend(added)

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
        identities = self._validate_identities(replacement)
        source = self._surface[start:end]
        source_ids = tuple(message.id for message in source)
        transcript_start = None
        if not preserve_transcript:
            origins = resolve_transcript_sources(source_ids, self._lineage)
            transcript_start = self._transcript_span(self._transcript, origins)
        if self._sink is not None:
            self._sink.replace_surface(
                source_ids,
                replacement,
                operation=operation,
                preserve_transcript=preserve_transcript,
            )
        self._seen_ids.update(identities)
        if preserve_transcript:
            self._lineage[replacement[0].id] = source_ids
        else:
            assert transcript_start is not None
            self._transcript[transcript_start:transcript_start + len(origins)] = replacement
            self._transcript_revision = uuid4().hex
        self._surface[start:end] = replacement
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

    def _validate_identities(self, messages: Sequence[ConversationMessage]) -> list[str]:
        identities = [message.id for message in messages]
        duplicates = self._seen_ids.intersection(identities)
        if len(identities) != len(set(identities)) or duplicates:
            raise ValueError("Conversation message identities must be unique")
        return identities

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
