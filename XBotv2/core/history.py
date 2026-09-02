"""Conversation history ownership and mutation contract."""

from __future__ import annotations

import base64
import binascii
import json
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from typing import Protocol, overload
from uuid import uuid4

from XBotv2.core.messages import Message
from pydantic import JsonValue


class HistoryCursorInvalid(ValueError):
    """The requested page does not belong to the current history revision."""


@dataclass(frozen=True, slots=True)
class ConversationPage:
    messages: tuple[Message, ...]
    next_cursor: str | None = None


@dataclass(frozen=True, slots=True)
class HistoryNode:
    """One message-producing node on the derived conversation surface."""

    node_id: str
    message: Message


class ConversationPageReader(Protocol):
    def page(
        self,
        *,
        limit: int,
        cursor: str | None = None,
    ) -> ConversationPage: ...


class HistorySink(Protocol):
    """Append-only trajectory boundary used by one conversation history."""

    def append(self, messages: Sequence[Message]) -> tuple[HistoryNode, ...]: ...

    def replace_surface(
        self,
        source_node_ids: Sequence[str],
        messages: Sequence[Message],
        *,
        operation: str,
        preserve_transcript: bool,
    ) -> tuple[HistoryNode, ...]: ...

    def record(self, event: str, data: dict[str, JsonValue]) -> None: ...


class ConversationHistory(Sequence[Message]):
    """The sole owner of the current, effective conversation history."""

    def __init__(
        self,
        messages: Iterable[Message] = (),
        *,
        sink: HistorySink | None = None,
        nodes: Iterable[HistoryNode] | None = None,
    ) -> None:
        initial = list(nodes) if nodes is not None else [
            HistoryNode(f"memory:{uuid4().hex}", message)
            for message in messages
        ]
        self._nodes = initial
        self._transcript = list(initial)
        self._lineage = {node.node_id: (node.node_id,) for node in initial}
        for node in self._nodes:
            node.message.seal()
        self._messages = [node.message for node in self._nodes]
        for message in self._messages:
            message.seal()
        self._sink = sink
        self._surface_revision = uuid4().hex
        self._transcript_revision = uuid4().hex

    def snapshot(self) -> tuple[Message, ...]:
        return tuple(self._messages)

    def node_ids(self) -> tuple[str, ...]:
        """Return stable identities for the current derived surface."""
        return tuple(node.node_id for node in self._nodes)

    def append(self, message: Message) -> None:
        self.extend((message,))

    def extend(self, messages: Iterable[Message]) -> None:
        added = tuple(messages)
        if not added:
            return
        nodes = (
            self._sink.append(added)
            if self._sink is not None
            else tuple(
                HistoryNode(f"memory:{uuid4().hex}", message)
                for message in added
            )
        )
        self._admit(nodes)
        self._nodes.extend(nodes)
        self._messages.extend(node.message for node in nodes)
        self._transcript.extend(nodes)
        self._lineage.update({node.node_id: (node.node_id,) for node in nodes})

    def replace(
        self,
        messages: Iterable[Message],
        *,
        operation: str = "replace",
    ) -> None:
        replacement = tuple(messages)
        if not self._nodes:
            self.extend(replacement)
            return
        self.replace_range(0, len(self._nodes), replacement, operation=operation)

    def replace_range(
        self,
        start: int,
        end: int,
        messages: Iterable[Message],
        *,
        operation: str,
        preserve_transcript: bool = False,
    ) -> None:
        """Replace one current contiguous surface span by appending an operation."""
        if start < 0 or end > len(self._nodes) or start >= end:
            raise ValueError("History replacement range must be non-empty and current")
        replacement = tuple(messages)
        if preserve_transcript and len(replacement) != 1:
            raise ValueError(
                "Transcript-preserving replacement must produce one surface node"
            )
        source = self._nodes[start:end]
        origins = tuple(
            origin
            for node in source
            for origin in self._lineage.get(node.node_id, (node.node_id,))
        )
        transcript = list(self._transcript)
        transcript_start = None
        if not preserve_transcript:
            transcript_start = self._transcript_span(transcript, origins)
        nodes = (
            self._sink.replace_surface(
                tuple(node.node_id for node in source),
                replacement,
                operation=operation,
                preserve_transcript=preserve_transcript,
            )
            if self._sink is not None
            else tuple(
                HistoryNode(f"memory:{uuid4().hex}", message)
                for message in replacement
            )
        )
        self._admit(nodes)
        if preserve_transcript:
            self._lineage[nodes[0].node_id] = origins
        else:
            assert transcript_start is not None
            transcript[transcript_start:transcript_start + len(origins)] = nodes
            self._lineage.update({node.node_id: (node.node_id,) for node in nodes})
            self._transcript = transcript
            self._transcript_revision = uuid4().hex
        self._nodes[start:end] = nodes
        self._messages[start:end] = [node.message for node in nodes]
        self._surface_revision = uuid4().hex

    @staticmethod
    def _transcript_span(
        transcript: list[HistoryNode],
        source_node_ids: Sequence[str],
    ) -> int:
        try:
            start = next(
                index
                for index, node in enumerate(transcript)
                if node.node_id == source_node_ids[0]
            )
        except StopIteration as exc:
            raise RuntimeError("History transcript sources are not current") from exc
        current = [
            node.node_id
            for node in transcript[start:start + len(source_node_ids)]
        ]
        if current != list(source_node_ids):
            raise RuntimeError("History transcript sources are not current")
        return start

    def record(self, event: str, data: dict[str, JsonValue]) -> None:
        """Append a log-only trajectory event without changing the surface."""
        if self._sink is not None:
            self._sink.record(event, data)

    @staticmethod
    def _admit(nodes: Sequence[HistoryNode]) -> None:
        for node in nodes:
            node.message.seal()

    def page(
        self,
        *,
        limit: int,
        cursor: str | None = None,
    ) -> ConversationPage:
        if limit < 1:
            raise ValueError("History page limit must be positive")
        end = (
            len(self._messages)
            if cursor is None
            else decode_history_cursor(cursor, self._surface_revision)
        )
        if end < 0 or end > len(self._messages):
            raise HistoryCursorInvalid(
                "History cursor is outside the current history"
            )
        start = max(0, end - limit)
        return ConversationPage(
            tuple(self._messages[start:end]),
            encode_history_cursor(self._surface_revision, start) if start else None,
        )

    def page_transcript(
        self,
        *,
        limit: int,
        cursor: str | None = None,
    ) -> ConversationPage:
        if limit < 1:
            raise ValueError("History page limit must be positive")
        end = (
            len(self._transcript)
            if cursor is None
            else decode_history_cursor(cursor, self._transcript_revision)
        )
        if end < 0 or end > len(self._transcript):
            raise HistoryCursorInvalid("History cursor is outside the transcript")
        start = max(0, end - limit)
        return ConversationPage(
            tuple(node.message for node in self._transcript[start:end]),
            encode_history_cursor(self._transcript_revision, start) if start else None,
        )

    def replace_last(self, message: Message) -> None:
        if not self._messages:
            raise IndexError("Cannot replace the last message of empty history")
        self.replace_range(
            len(self._messages) - 1,
            len(self._messages),
            (message,),
            operation="replace_last",
        )

    def undo(self, turns: int) -> tuple[Message, ...]:
        if turns < 1:
            raise ValueError("Undo turns must be positive")
        user_indexes = [
            index
            for index, message in enumerate(self._messages)
            if message.role == "user"
        ]
        if turns > len(user_indexes):
            raise ValueError(
                f"Cannot undo {turns} turns; history has {len(user_indexes)}."
            )
        self.replace_range(
            user_indexes[-turns],
            len(self._messages),
            (),
            operation="undo",
        )
        return self.snapshot()

    def clear(self) -> None:
        if self._nodes:
            self.replace_range(0, len(self._nodes), (), operation="clear")

    @overload
    def __getitem__(self, index: int) -> Message: ...

    @overload
    def __getitem__(self, index: slice) -> list[Message]: ...

    def __getitem__(self, index: int | slice) -> Message | list[Message]:
        return self._messages[index]

    def __iter__(self) -> Iterator[Message]:
        return iter(self._messages)

    def __len__(self) -> int:
        return len(self._messages)

    def __eq__(self, other: object) -> bool:
        if isinstance(other, ConversationHistory):
            return self._messages == other._messages
        if isinstance(other, Sequence):
            return self._messages == list(other)
        return NotImplemented

    def __repr__(self) -> str:
        return repr(self._messages)


def encode_history_cursor(revision: str, offset: int) -> str:
    value = json.dumps([1, revision, offset], separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def decode_history_cursor(cursor: str, revision: str) -> int:
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
    "ConversationPage",
    "ConversationPageReader",
    "HistoryCursorInvalid",
    "HistorySink",
    "decode_history_cursor",
    "encode_history_cursor",
]
