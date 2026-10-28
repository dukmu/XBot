"""Durable two-target input queue owned by an agent loop."""

from __future__ import annotations

import asyncio
import hashlib
import json
from contextlib import AsyncExitStack, nullcontext
from XBotv2.persistence.api.models import CommitUncertain
from collections import deque
from collections.abc import Iterable, Iterator, Sequence
from XBotv2.agentloop.api.contracts import (
    Claimed,
    Consumed,
    Discarded,
    Edited,
    HumanInput,
    InboxChange,
    InboxItem,
    InboxMutation,
    InboxSink,
    Inserted,
    Removed,
    Retargeted,
)
from xcore.api.events import EventDispatch
from XBotv2.agentloop.api.events import Events, ObserveInbox, PrepareInput
from XBotv2.core.api.artifacts import ArtifactStorePort
from XBotv2.core.api.domain import InboxTarget


class AgentInbox:
    """A two-target FIFO for model-visible human and runtime inputs."""

    def __init__(
        self,
        *,
        events: EventDispatch,
        sink: InboxSink,
        items: Iterable[InboxItem] = (),
        artifacts: ArtifactStorePort | None = None,
    ) -> None:
        self._next_turn: deque[InboxItem] = deque()
        self._next_step: deque[InboxItem] = deque()
        self._ids: set[str] = set()
        self._claimed_ids: set[str] = set()
        self._observers: set[asyncio.Event] = set()
        self._lock = asyncio.Lock()
        self._artifacts = artifacts
        self._sink = sink
        self._events = events
        for item in items:
            if item.id in self._ids:
                raise ValueError(f"Duplicate restored inbox id: {item.id}")
            self._queue(item.target).append(item)
            self._ids.add(item.id)

    async def _synchronize(self):
        # SQL recovery can complete a previously uncertain accept/edit before
        # the next caller. Refresh only pending delivery state; claims still
        # belong to their in-flight turn, including an already committed body.
        pending = await self._sink.load()
        restored = {item.id for item in pending} - self._ids
        claimed = [item for item in self._items() if item.id in self._claimed_ids]
        self._next_step.clear()
        self._next_turn.clear()
        self._ids.clear()
        for item in [
            *claimed,
            *(item for item in pending if item.id not in self._claimed_ids),
        ]:
            self._queue(item.target).append(item)
            self._ids.add(item.id)
        self._notify_observers()
        return restored

    async def _prepare(self, item, resources, *, editing=False):
        result = await self._events.serial(
            Events.PREPARE_INPUT, PrepareInput(item, resources, editing=editing),
        )
        if result is None:
            return item
        if not isinstance(result, PrepareInput):
            raise TypeError("PREPARE_INPUT must return PrepareInput or None")
        if result.resources is not resources:
            raise ValueError("PREPARE_INPUT cannot replace its resource scope")
        candidate = result.item
        original_payload = candidate.input.model_copy(update={
            "content": item.input.content,
            "artifacts": item.input.artifacts,
        })
        if (
            candidate.model_copy(update={"input": item.input}) != item
            or type(candidate.input) is not type(item.input)
            or original_payload != item.input
        ):
            raise ValueError("PREPARE_INPUT may only change content and artifacts")
        return candidate

    async def submit(
        self, item: InboxItem, *, wake: bool, resources: AsyncExitStack | None = None,
    ) -> bool:
        async with self._artifacts.publication() if self._artifacts else nullcontext():
            return await self._submit(item, wake=wake, resources=resources)

    async def _submit(self, item, *, wake, resources):
        async with self._lock:
            restored = await self._synchronize()
            content_sha256 = content_digest(item.input.content)
            fingerprint = input_fingerprint(
                item.input, content_sha256=content_sha256,
            )
            if await self._sink.accepted(item.id, fingerprint):
                return item.id in restored
            uncertain = None
            async with resources.pop_all() if resources is not None else AsyncExitStack() as resources:
                item = await self._prepare(item, resources)
                change = Inserted(
                    item=item, wake=wake, request_fingerprint=fingerprint,
                    content_sha256=content_sha256,
                )
                try:
                    appended = await self._sink.append(change)
                except CommitUncertain as error:
                    # The durable log can already own this object. Finish the
                    # publication scopes normally, then report the uncertainty.
                    uncertain = error
            if uncertain is not None:
                raise uncertain
            if not appended:
                return item.id in restored
            self._queue(item.target).append(item)
            self._ids.add(item.id)
            await self._record(change)
            return True

    async def claim_turn(self) -> list[InboxItem]:
        async with self._lock:
            await self._synchronize()
            items = list(self._unclaimed(self._next_step))
            next_turn = next(self._unclaimed(self._next_turn), None)
            if next_turn is not None:
                items.append(next_turn)
            return await self._claim(items)

    async def claim_step(self) -> list[InboxItem]:
        async with self._lock:
            await self._synchronize()
            items = list(self._unclaimed(self._next_step))
            return await self._claim(items)

    async def _claim(self, items: list[InboxItem]) -> list[InboxItem]:
        # Both callers hold the queue lock. A failed handoff must release only
        # this batch, not claims already owned by the running turn.
        if not items:
            return []
        claimed_ids = {item.id for item in items}
        self._claimed_ids.update(claimed_ids)
        try:
            await self._record(Claimed(ids=tuple(item.id for item in items)))
        except BaseException:
            self._claimed_ids.difference_update(claimed_ids)
            raise
        return items

    async def commit(self, input_ids: Sequence[str]) -> None:
        committed = set(input_ids)
        if not committed:
            return
        async with self._lock:
            unknown = committed - self._claimed_ids
            if unknown:
                raise ValueError(
                    "Cannot commit unclaimed inbox ids: " + ", ".join(sorted(unknown))
                )
            items = [item for item in self._items() if item.id in committed]
            change = Consumed(ids=tuple(item.id for item in items))
            await self._sink.append(change)
            self._remove_ids(committed)
            self._claimed_ids.difference_update(committed)
            await self._record(change)

    async def reconcile(self, committed_ids: set[str]) -> None:
        """Release all outstanding claims at the end of the owning loop turn."""
        async with self._lock:
            durable = self._claimed_ids & committed_ids
            consumed = tuple(item for item in self._items() if item.id in durable)
            if durable:
                await self._sink.append(
                    Consumed(ids=tuple(item.id for item in consumed))
                )
                self._remove_ids(durable)
            self._claimed_ids.clear()
            if consumed:
                await self._record(Consumed(ids=tuple(item.id for item in consumed)))

    async def edit(self, input_id: str, content: str) -> InboxItem:
        async with self._artifacts.publication() if self._artifacts else nullcontext():
            return await self._edit(input_id, content)

    async def _edit(self, input_id: str, content: str) -> InboxItem:
        if not content.strip():
            raise ValueError("Inbox input content cannot be empty")
        async with self._lock:
            await self._synchronize()
            current = self._pending_item(input_id)
            if not isinstance(current.input, HumanInput):
                raise ValueError("Only human inputs can be edited")
            digest = content_digest(content)
            if await self._sink.current_content_sha256(input_id) == digest:
                return current
            updated = current.model_copy(
                update={"input": current.input.model_copy(update={"content": content})}
            )
            uncertain = None
            async with AsyncExitStack() as resources:
                updated = await self._prepare(updated, resources, editing=True)
                change = Edited(
                    id=current.id, content=updated.input.content,
                    artifacts=updated.input.artifacts, content_sha256=digest,
                )
                try:
                    await self._sink.append(change)
                except CommitUncertain as error:
                    uncertain = error
            if uncertain is not None:
                raise uncertain
            queue = self._queue(current.target)
            queue[queue.index(current)] = updated
            await self._record(change)
            return updated

    async def remove(self, input_id: str) -> InboxItem:
        async with self._lock:
            await self._synchronize()
            current = self._pending_item(input_id)
            change = Removed(id=current.id)
            await self._sink.append(change)
            self._queue(current.target).remove(current)
            self._ids.remove(input_id)
            await self._record(change)
            return current

    async def retarget(self, input_id: str, target: InboxTarget | str) -> InboxItem:
        target = InboxTarget(target)
        async with self._lock:
            await self._synchronize()
            current = self._pending_item(input_id)
            if current.target is target:
                return current
            updated = current.model_copy(update={"target": target})
            change = Retargeted(id=current.id, target=target)
            await self._sink.append(change)
            self._queue(current.target).remove(current)
            self._queue(target).append(updated)
            await self._record(change)
            return updated

    async def discard(self) -> list[InboxItem]:
        async with self._lock:
            await self._synchronize()
            items = [*self._next_step, *self._next_turn]
            if not items:
                return []
            change = Discarded(ids=tuple(item.id for item in items))
            await self._sink.append(change)
            self._next_step.clear()
            self._next_turn.clear()
            self._ids.clear()
            self._claimed_ids.clear()
            await self._record(change)
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

    def _remove_ids(self, input_ids: set[str]) -> None:
        self._next_step = deque(
            item for item in self._next_step if item.id not in input_ids
        )
        self._next_turn = deque(
            item for item in self._next_turn if item.id not in input_ids
        )
        self._ids.difference_update(input_ids)

    async def _record(self, change: InboxChange) -> None:
        self._notify_observers()
        await self._events.emit(Events.INBOX_CHANGED, ObserveInbox(change))

    def _notify_observers(self) -> None:
        for observer in self._observers:
            observer.set()

    def subscribe(self) -> asyncio.Event:
        observer = asyncio.Event()
        self._observers.add(observer)
        return observer

    def unsubscribe(self, observer: asyncio.Event) -> None:
        self._observers.discard(observer)

    def __len__(self) -> int:
        return sum(1 for _ in self._unclaimed(self._items()))

    @property
    def pending(self) -> list[InboxItem]:
        return list(self._unclaimed(self._items()))


def content_digest(content: str) -> str:
    digest = hashlib.sha256()
    for offset in range(0, len(content), 65536):
        digest.update(content[offset : offset + 65536].encode("utf-8"))
    return digest.hexdigest()


def input_fingerprint(payload, *, content_sha256=None):
    """Fingerprint original request fields, independently of body projection policy."""
    metadata = payload.model_dump(mode="json", exclude={"content"})
    metadata["content_sha256"] = content_sha256 or content_digest(payload.content)
    encoded = json.dumps(
        metadata, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
