"""Conversation history ownership and mutation contract."""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Sequence
from typing import Protocol, overload

from XBotv2.core.messages import Message


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


__all__ = ["ConversationHistory", "HistorySink"]
