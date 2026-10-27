"""Effective conversation working set and transcript lineage."""

from __future__ import annotations
import asyncio
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
        if len({message.id for message in self._surface}) != len(self._surface):
            raise ValueError("Conversation message identities must be unique")
        self._sink = sink
        self._storage_version = None
        self._mutation_lock = asyncio.Lock()
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

    async def synchronize(self):
        if self._sink is None:
            return
        state = await self._sink.surface_state(self._storage_version)
        if state is not None:
            self._storage_version, messages = state
            self._surface = list(messages)
            self._surface_revision = uuid4().hex
            self._transcript_revision = uuid4().hex

    async def append(self, message: ConversationMessage) -> None:
        await self.extend((message,))

    async def extend(self, messages: Iterable[ConversationMessage]) -> None:
        added = tuple(messages)
        if not added:
            return
        async with self._mutation_lock:
            await self.synchronize()
            self._validate_identities(added)
            if self._sink is not None:
                try:
                    self._storage_version = await self._sink.append(
                        added, expected_version=self._storage_version
                    )
                except BaseException as error:
                    try:
                        await self.synchronize()
                    except Exception as recovery_error:
                        raise error from recovery_error
                    raise
            self._surface.extend(added)
            self._surface_revision = uuid4().hex

    async def replace(
        self,
        messages: Iterable[ConversationMessage],
        *,
        operation: str = "replace",
    ) -> None:
        replacement = tuple(messages)
        if not self._surface:
            await self.extend(replacement)
            return
        await self.replace_range(
            0, len(self._surface), replacement, operation=operation
        )

    async def replace_range(
        self,
        start: int,
        end: int,
        messages: Iterable[ConversationMessage],
        *,
        operation: str,
        preserve_transcript: bool = False,
    ) -> None:
        """Replace a current span only if its pre-await revision still applies."""
        proposed_revision = self.surface_revision
        async with self._mutation_lock:
            await self.synchronize()
            if proposed_revision != self.surface_revision:
                raise ValueError("History changed before replacement commit")
            if start < 0 or end > len(self._surface) or start >= end:
                raise ValueError(
                    "History replacement range must be non-empty and current"
                )
            replacement = tuple(messages)
            if preserve_transcript and len(replacement) != 1:
                raise ValueError(
                    "Transcript-preserving replacement must produce one surface record"
                )
            identities = self._validate_identities(replacement)
            source = self._surface[start:end]
            source_ids = tuple(message.id for message in source)
            if self._sink is not None:
                try:
                    self._storage_version = await self._sink.replace_surface(
                        source_ids,
                        replacement,
                        operation=operation,
                        preserve_transcript=preserve_transcript,
                        expected_version=self._storage_version,
                    )
                except BaseException as error:
                    try:
                        await self.synchronize()
                    except Exception as recovery_error:
                        raise error from recovery_error
                    raise
            if not preserve_transcript:
                self._transcript_revision = uuid4().hex
            self._surface[start:end] = replacement
            self._surface_revision = uuid4().hex

    async def begin_turn(self, input_id: str) -> int | None:
        if self._sink is not None:
            return await self._sink.begin_turn(input_id)
        return None

    async def record(self, event: DurableEvent, *, durable: bool = False) -> None:
        """Append a log-only trajectory event without changing the surface."""
        if self._sink is not None:
            await self._sink.record(event, durable=durable)

    async def open_transactions(
        self,
        transaction_kind: str,
    ) -> frozenset[str]:
        """Return durable starts that do not yet have a correlated end."""
        if self._sink is None:
            return frozenset()
        return await self._sink.open_transactions(transaction_kind)

    def _validate_identities(
        self, messages: Sequence[ConversationMessage]
    ) -> list[str]:
        identities = [message.id for message in messages]
        duplicates = {message.id for message in self._surface}.intersection(identities)
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

    async def page_transcript(
        self,
        *,
        limit: int,
        cursor: Cursor | None = None,
    ) -> HistoryPage[ConversationMessage]:
        if self._sink is None:
            return self.page(limit=limit, cursor=cursor)
        return await self._sink.page_transcript(limit=limit, cursor=cursor)

    async def replace_last(self, message: ConversationMessage) -> None:
        if not self._surface:
            raise IndexError("Cannot replace the last message of empty history")
        await self.replace_range(
            len(self._surface) - 1,
            len(self._surface),
            (message,),
            operation="replace_last",
        )

    async def undo(self, turns: int) -> tuple[ConversationMessage, ...]:
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
        await self.replace_range(
            user_indexes[-turns],
            len(self._surface),
            (),
            operation="undo",
        )
        return self.snapshot()

    async def clear(self) -> None:
        if self._surface:
            await self.replace_range(0, len(self._surface), (), operation="clear")

    @overload
    def __getitem__(self, index: int) -> ConversationMessage: ...

    @overload
    def __getitem__(self, index: slice) -> list[ConversationMessage]: ...

    def __getitem__(
        self, index: int | slice
    ) -> ConversationMessage | list[ConversationMessage]:
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
