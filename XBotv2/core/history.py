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


class HistoryCursorInvalid(ValueError):
    """The requested page does not belong to the current history revision."""


@dataclass(frozen=True, slots=True)
class ConversationPage:
    messages: tuple[Message, ...]
    next_cursor: str | None = None


class ConversationPageReader(Protocol):
    def page(
        self,
        *,
        limit: int,
        cursor: str | None = None,
    ) -> ConversationPage: ...


class HistorySink(Protocol):
    """Durability boundary used by one conversation history."""

    def append(self, messages: Sequence[Message]) -> None: ...

    def replace(self, messages: Sequence[Message]) -> None: ...


class ConversationHistory(Sequence[Message]):
    """The sole owner of the current, effective conversation history."""

    def __init__(
        self,
        messages: Iterable[Message] = (),
        *,
        sink: HistorySink | None = None,
    ) -> None:
        self._messages = list(messages)
        for message in self._messages:
            message.seal()
        self._sink = sink
        self._revision = uuid4().hex

    def snapshot(self) -> tuple[Message, ...]:
        return tuple(self._messages)

    def append(self, message: Message) -> None:
        self.extend((message,))

    def extend(self, messages: Iterable[Message]) -> None:
        added = tuple(messages)
        if not added:
            return
        if self._sink is not None:
            self._sink.append(added)
        for message in added:
            message.seal()
        self._messages.extend(added)

    def replace(self, messages: Iterable[Message]) -> None:
        replacement = tuple(messages)
        if self._sink is not None:
            self._sink.replace(replacement)
        for message in replacement:
            message.seal()
        self._messages = list(replacement)
        self._revision = uuid4().hex

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
            else decode_history_cursor(cursor, self._revision)
        )
        if end < 0 or end > len(self._messages):
            raise HistoryCursorInvalid(
                "History cursor is outside the current history"
            )
        start = max(0, end - limit)
        return ConversationPage(
            tuple(self._messages[start:end]),
            encode_history_cursor(self._revision, start) if start else None,
        )

    def replace_last(self, message: Message) -> None:
        if not self._messages:
            raise IndexError("Cannot replace the last message of empty history")
        self.replace((*self._messages[:-1], message))

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
        self.replace(self._messages[:user_indexes[-turns]])
        return self.snapshot()

    def clear(self) -> None:
        self.replace(())

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
