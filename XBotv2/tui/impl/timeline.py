"""The conversation timeline: stable ids, in-place upsert, explicit windows.

This replaces the old ``TuiState.transcript`` model, which keyed entries by their
decimal index in a backing list and told the renderer what happened through a set
of eviction/insertion counters. Both of those are gone:

* entries carry a stable id, so an update is an upsert and nothing is renumbered;
* a window is a *view* over ids, so where the reader looks can never change what
  the state holds, and nothing is dropped when they scroll up.

Nothing here imports Textual; the whole model is a plain data structure.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field
from enum import Enum
from itertools import islice
from typing import Any, ClassVar, Iterator, Literal, Mapping

from pydantic import JsonValue


class EntryKind(Enum):
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"
    NOTICE = "notice"
    ERROR = "error"


class Delivery(Enum):
    """Where a submitted user message stands.

    ``PENDING`` is set the moment the client accepts the input, so the user sees
    their own message before the server echoes it; the server's frame upgrades it
    to ``ACCEPTED`` (or the submission failure marks it ``FAILED``).
    """

    PENDING = "pending"
    ACCEPTED = "accepted"
    FAILED = "failed"


# The canonical tool outcome vocabulary projected for display, plus the two
# states a tool passes through before its result arrives. Reusing the wire words
# means a tool_result, a resumed history record, and a client-side "still running"
# row all speak one vocabulary, with no translation table to drift.
ToolStatus = Literal["pending", "running", "success", "error", "denied", "cancelled"]

_UNFINISHED_TOOL_STATUSES: frozenset[str] = frozenset({"pending", "running"})


@dataclass(frozen=True)
class Entry:
    """Common identity of every timeline entry.

    ``id`` is the primary key and must be stable for the life of the entry. It is
    owned by whatever created the entry: a record read from the server keeps the
    identity of its transcript node, a submitted prompt keeps the id the client
    submitted it under until the server names the record, and everything the
    client invents (a notice, an error, a gap) is prefixed ``local:``. Nothing
    merges entries across those worlds: a server record replaces the timeline
    rather than being matched into it.

    Position is insertion order and nothing else -- there is no second counter
    that could disagree with it.
    """

    id: str

    kind: ClassVar[EntryKind]


@dataclass(frozen=True)
class UserEntry(Entry):
    content: str = ""
    delivery: Delivery = Delivery.PENDING

    kind: ClassVar[EntryKind] = EntryKind.USER


@dataclass(frozen=True)
class AssistantEntry(Entry):
    content: str = ""
    reasoning: str = ""
    streaming: bool = False

    kind: ClassVar[EntryKind] = EntryKind.ASSISTANT


@dataclass(frozen=True)
class ToolEntry(Entry):
    """One tool call. ``args`` and ``result`` hold the server's payload as sent;
    formatting them is the view's job, so nothing is decoded before it is read."""

    name: str = "tool"
    call_id: str = ""
    args: Mapping[str, Any] = field(default_factory=dict, hash=False)
    status: ToolStatus = "pending"
    result: JsonValue = ""
    started_at: float = 0.0
    finished_at: float = 0.0

    kind: ClassVar[EntryKind] = EntryKind.TOOL


@dataclass(frozen=True)
class NoticeEntry(Entry):
    notice_kind: str = "info"
    text: str = ""
    detail: str = ""
    level: str = "info"

    kind: ClassVar[EntryKind] = EntryKind.NOTICE


@dataclass(frozen=True)
class ErrorEntry(Entry):
    message: str = ""

    kind: ClassVar[EntryKind] = EntryKind.ERROR


@dataclass(frozen=True)
class TimelineWindow:
    """Which ids a view should currently render.

    ``newer_count`` is how many entries sit below the window (the tail included).
    It is not "how much was dropped": nothing is ever dropped by a window.
    """

    ids: tuple[str, ...]
    start_index: int
    end_index: int
    newer_count: int
    total: int

    @property
    def at_tail(self) -> bool:
        return self.newer_count == 0


class Timeline:
    """Insertion-ordered entries, addressed by stable id."""

    def __init__(self) -> None:
        self._entries: OrderedDict[str, Entry] = OrderedDict()

    # --- identity -----------------------------------------------------
    def upsert(self, entry: Entry) -> None:
        """Insert or replace by id.

        Replacing never moves an entry: this is what lets a streamed answer grow
        in place while a user interjection stays where it was inserted.
        """
        self._entries[entry.id] = entry

    def prepend(self, entries: Iterable[Entry]) -> tuple[str, ...]:
        """Insert older entries ahead of everything held; return the new ids.

        A loaded page is strictly older than the window it extends, so its order
        is the reader's order. An entry the timeline already holds is *not* moved
        to the front -- it is replaced where it stands, because a reader looking
        at it must not see it jump. Ids already present are therefore left out of
        the returned tuple: what comes back is exactly what the timeline gained.
        """
        page = [entry for entry in entries if entry.id not in self._entries]
        for entry in entries:
            self.upsert(entry)
        if not page:
            return ()
        gained = {entry.id for entry in page}
        self._entries = OrderedDict(
            [(entry.id, entry) for entry in page]
            + [
                (entry_id, entry)
                for entry_id, entry in self._entries.items()
                if entry_id not in gained
            ]
        )
        return tuple(entry.id for entry in page)

    def remove(self, entry_id: str) -> bool:
        return self._entries.pop(entry_id, None) is not None

    def replace_identity(self, previous_id: str, entry: Entry) -> None:
        """Replace one entry and its id without changing transcript order."""
        if previous_id == entry.id:
            self.upsert(entry)
            return
        if previous_id not in self._entries:
            raise KeyError(previous_id)
        if entry.id in self._entries:
            raise ValueError(f"Timeline already contains {entry.id!r}")
        self._entries = OrderedDict(
            (entry.id, entry) if entry_id == previous_id else (entry_id, value)
            for entry_id, value in self._entries.items()
        )

    def get(self, entry_id: str) -> Entry | None:
        return self._entries.get(entry_id)

    def ids(self) -> tuple[str, ...]:
        return tuple(self._entries)

    def tail_id(self) -> str | None:
        return next(reversed(self._entries), None)

    def index_of(self, entry_id: str) -> int:
        """Position of an id, or ``KeyError`` if the timeline no longer holds it."""
        for index, candidate in enumerate(self._entries):
            if candidate == entry_id:
                return index
        raise KeyError(entry_id)

    def __len__(self) -> int:
        return len(self._entries)

    def __contains__(self, entry_id: object) -> bool:
        return entry_id in self._entries

    def __iter__(self) -> Iterator[Entry]:
        return iter(self._entries.values())

    # --- windows ------------------------------------------------------
    def window(self, *, size: int, end: str | None = None) -> TimelineWindow:
        """The last ``size`` entries ending at ``end`` (inclusive), or at the tail.

        An unknown ``end`` raises instead of falling back to the tail: silently
        re-anchoring is what used to move the reader under their own feet.
        """
        total = len(self._entries)
        end_index = total if end is None else self.index_of(end) + 1
        start_index = max(0, end_index - max(0, size))
        ids = tuple(islice(self._entries, start_index, end_index))
        return TimelineWindow(
            ids=ids,
            start_index=start_index,
            end_index=end_index,
            newer_count=total - end_index,
            total=total,
        )


__all__ = [
    "AssistantEntry",
    "Delivery",
    "Entry",
    "EntryKind",
    "ErrorEntry",
    "NoticeEntry",
    "Timeline",
    "TimelineWindow",
    "ToolEntry",
    "ToolStatus",
    "UserEntry",
]
