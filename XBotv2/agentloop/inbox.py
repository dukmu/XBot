"""Durable two-target input queue owned by an agent loop."""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Iterable, Iterator, Sequence

from XBotv2.agentloop.contracts import (
    Claimed,
    Consumed,
    Discarded,
    Edited,
    HumanInput,
    InboxChange,
    InboxItem,
    InboxSink,
    InboxTarget,
    Inserted,
    Removed,
    Retargeted,
)
from XBotv2.agentloop.events import EventPort, Events, ObserveInbox


class EphemeralInboxSink:
    """Explicit persistence policy for sessions without thread storage."""

    def replace(self, items: Sequence[InboxItem]) -> None:
        del items


class AgentInbox:
    """A two-target FIFO for model-visible human and runtime inputs."""

    def __init__(
        self,
        *,
        events: EventPort,
        sink: InboxSink,
        items: Iterable[InboxItem] = (),
    ) -> None:
        self._next_turn: deque[InboxItem] = deque()
        self._next_step: deque[InboxItem] = deque()
        self._ids: set[str] = set()
        self._claimed_ids: set[str] = set()
        self._lock = asyncio.Lock()
        self._sink = sink
        self._events = events
        for item in items:
            if item.id in self._ids:
                raise ValueError(f"Duplicate restored inbox id: {item.id}")
            self._queue(item.target).append(item)
            self._ids.add(item.id)

    async def submit(self, item: InboxItem, *, wake: bool) -> None:
        async with self._lock:
            if item.id in self._ids:
                raise ValueError(f"Duplicate inbox input id: {item.id}")
            self._replace_snapshot([*self._items(), item])
            self._queue(item.target).append(item)
            self._ids.add(item.id)
            await self._record(Inserted(item=item, wake=wake))

    async def claim_turn(self) -> list[InboxItem]:
        async with self._lock:
            items = list(self._unclaimed(self._next_step))
            next_turn = next(self._unclaimed(self._next_turn), None)
            if next_turn is not None:
                items.append(next_turn)
            if not items:
                return []
            self._claimed_ids.update(item.id for item in items)
            await self._record(Claimed(items=tuple(items)))
            return items

    async def claim_step(self) -> list[InboxItem]:
        async with self._lock:
            items = list(self._unclaimed(self._next_step))
            if not items:
                return []
            self._claimed_ids.update(item.id for item in items)
            await self._record(Claimed(items=tuple(items)))
            return items

    async def commit(self, input_ids: Sequence[str]) -> None:
        committed = set(input_ids)
        if not committed:
            return
        async with self._lock:
            unknown = committed - self._claimed_ids
            if unknown:
                raise ValueError("Cannot commit unclaimed inbox ids: " + ", ".join(sorted(unknown)))
            items = [item for item in self._items() if item.id in committed]
            await self._record(Consumed(items=tuple(items)))
            self._remove_ids(committed)
            self._claimed_ids.difference_update(committed)

    async def reconcile(self, input_ids: Sequence[str], committed_ids: set[str]) -> None:
        requested = set(input_ids)
        async with self._lock:
            active = requested & self._claimed_ids
            durable = active & committed_ids
            if durable:
                self._remove_ids(durable)
            self._claimed_ids.difference_update(active)

    async def edit(self, input_id: str, content: str) -> InboxItem:
        if not content.strip():
            raise ValueError("Inbox input content cannot be empty")
        async with self._lock:
            current = self._pending_item(input_id)
            if not isinstance(current.input, HumanInput):
                raise ValueError("Only human inputs can be edited")
            updated = current.model_copy(update={"input": current.input.model_copy(update={"content": content})})
            self._replace_snapshot([updated if item.id == input_id else item for item in self._items()])
            queue = self._queue(current.target)
            queue[queue.index(current)] = updated
            await self._record(Edited(previous=current, current=updated))
            return updated

    async def remove(self, input_id: str) -> InboxItem:
        async with self._lock:
            current = self._pending_item(input_id)
            self._replace_snapshot([item for item in self._items() if item.id != input_id])
            self._queue(current.target).remove(current)
            self._ids.remove(input_id)
            await self._record(Removed(item=current))
            return current

    async def retarget(self, input_id: str, target: InboxTarget | str) -> InboxItem:
        target = InboxTarget(target)
        async with self._lock:
            current = self._pending_item(input_id)
            if current.target is target:
                return current
            updated = current.model_copy(update={"target": target})
            remaining = [item for item in self._items() if item.id != input_id]
            next_step = [item for item in remaining if item.target is InboxTarget.NEXT_STEP]
            next_turn = [item for item in remaining if item.target is InboxTarget.NEXT_TURN]
            (next_step if target is InboxTarget.NEXT_STEP else next_turn).append(updated)
            self._replace_snapshot([*next_step, *next_turn])
            self._queue(current.target).remove(current)
            self._queue(target).append(updated)
            await self._record(Retargeted(previous=current, current=updated))
            return updated

    async def discard(self) -> list[InboxItem]:
        async with self._lock:
            items = [*self._next_step, *self._next_turn]
            if not items:
                return []
            self._replace_snapshot([])
            self._next_step.clear()
            self._next_turn.clear()
            self._ids.clear()
            self._claimed_ids.clear()
            await self._record(Discarded(items=tuple(items)))
            return items

    def _queue(self, target: InboxTarget) -> deque[InboxItem]:
        return self._next_turn if target is InboxTarget.NEXT_TURN else self._next_step

    def _items(self) -> list[InboxItem]:
        return [*self._next_step, *self._next_turn]

    def _unclaimed(self, items: Iterable[InboxItem]) -> Iterator[InboxItem]:
        return (item for item in items if item.id not in self._claimed_ids)

    def _pending_item(self, input_id: str) -> InboxItem:
        if input_id in self._claimed_ids:
            raise KeyError(input_id)
        item = next((item for item in self._items() if item.id == input_id), None)
        if item is None:
            raise KeyError(input_id)
        return item

    def _replace_snapshot(self, items: Sequence[InboxItem]) -> None:
        self._sink.replace(items)

    def _remove_ids(self, input_ids: set[str]) -> None:
        remaining = [item for item in self._items() if item.id not in input_ids]
        self._replace_snapshot(remaining)
        self._next_step = deque(item for item in self._next_step if item.id not in input_ids)
        self._next_turn = deque(item for item in self._next_turn if item.id not in input_ids)
        self._ids.difference_update(input_ids)

    async def _record(self, change: InboxChange) -> None:
        await self._events.emit(Events.INBOX_CHANGED, ObserveInbox(change))

    def __len__(self) -> int:
        return sum(1 for _ in self._unclaimed(self._items()))

    @property
    def pending(self) -> list[InboxItem]:
        return list(self._unclaimed(self._items()))


__all__ = ["AgentInbox", "EphemeralInboxSink"]
