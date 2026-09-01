"""Agent-owned input inbox for the concrete loop driver.

The inbox is the only queue that stores model-visible input.  It follows the
DSH two-target contract: ``next-turn`` inputs start future turns, while
``next-step`` inputs are claimed between model/tool steps.  Transport code may
track response waiters by message id, but must never duplicate input content.
"""

from __future__ import annotations

import asyncio
import uuid
from collections import deque
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any, Protocol

from XBotv2.core.artifacts import ArtifactRef
from XBotv2.core.messages import ImageContent


class InboxTarget(str, Enum):
    NEXT_TURN = "next-turn"
    NEXT_STEP = "next-step"


@dataclass(slots=True)
class InboxInput:
    """One uniquely identified model-visible input."""

    content: str
    target: InboxTarget
    source: str = "user"
    message_id: str = field(default_factory=lambda: f"msg-{uuid.uuid4().hex}")
    images: list[ImageContent] = field(default_factory=list)
    artifacts: list[ArtifactRef] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


SpliceRecorder = Callable[[dict[str, Any]], Awaitable[None]]
WakeDriver = Callable[[], None]


class InboxSink(Protocol):
    def replace(self, items: Sequence[InboxInput]) -> None: ...


class AgentInbox:
    """Two FIFO input lists owned by one agent loop.

    Durable pending inputs are written before the in-memory projection changes.
    Splice notifications are runtime observations and do not own recovery.
    """

    def __init__(
        self,
        *,
        items: Iterable[InboxInput] = (),
        sink: InboxSink | None = None,
        record_splice: SpliceRecorder | None = None,
        wake_driver: WakeDriver | None = None,
    ) -> None:
        self._next_turn: deque[InboxInput] = deque()
        self._next_step: deque[InboxInput] = deque()
        self._ids: set[str] = set()
        self._claimed_ids: set[str] = set()
        self._lock = asyncio.Lock()
        self._sink = sink
        self._record_splice = record_splice
        self._wake_driver = wake_driver
        for item in items:
            if item.message_id in self._ids:
                raise ValueError(f"Duplicate restored inbox id: {item.message_id}")
            self._queue(item.target).append(item)
            self._ids.add(item.message_id)

    def set_wake_driver(self, wake_driver: WakeDriver | None) -> None:
        self._wake_driver = wake_driver

    async def send(
        self,
        content: str,
        *,
        target: InboxTarget | str,
        wakeup: bool,
        source: str = "user",
        message_id: str = "",
        images: list[ImageContent] | None = None,
        artifacts: list[ArtifactRef] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> InboxInput:
        target = InboxTarget(target)
        item = InboxInput(
            content=content,
            target=target,
            source=source,
            message_id=message_id or f"msg-{uuid.uuid4().hex}",
            images=list(images or []),
            artifacts=list(artifacts or []),
            metadata=dict(metadata or {}),
        )
        async with self._lock:
            if item.message_id in self._ids:
                raise ValueError(f"Duplicate inbox message id: {item.message_id}")
            self._persist([*self._items(), item])
            self._queue(target).append(item)
            self._ids.add(item.message_id)
            await self._record("insert", target, [item])
        if wakeup and self._wake_driver is not None:
            self._wake_driver()
        return item

    async def followup(self, content: str, **kwargs: Any) -> InboxInput:
        """Append to ``next-turn`` and wake the driver."""
        return await self.send(
            content,
            target=InboxTarget.NEXT_TURN,
            wakeup=True,
            **kwargs,
        )

    async def steer(self, content: str, **kwargs: Any) -> InboxInput:
        """Append to ``next-step`` and wake the driver."""
        return await self.send(
            content,
            target=InboxTarget.NEXT_STEP,
            wakeup=True,
            **kwargs,
        )

    async def inject(self, content: str, **kwargs: Any) -> InboxInput:
        """Append to ``next-step`` without waking the driver."""
        return await self.send(
            content,
            target=InboxTarget.NEXT_STEP,
            wakeup=False,
            **kwargs,
        )

    async def claim_turn(self) -> list[InboxInput]:
        """Atomically claim every next-step input and one next-turn input."""
        async with self._lock:
            items = [
                item for item in self._next_step
                if item.message_id not in self._claimed_ids
            ]
            next_turn = next(
                (
                    item for item in self._next_turn
                    if item.message_id not in self._claimed_ids
                ),
                None,
            )
            if next_turn is not None:
                items.append(next_turn)
            if not items:
                return []
            self._claimed_ids.update(item.message_id for item in items)
            await self._record("claim", None, items)
            return items

    async def claim_step(self) -> list[InboxInput]:
        """Atomically claim only inputs targeted at the next loop step."""
        async with self._lock:
            items = [
                item for item in self._next_step
                if item.message_id not in self._claimed_ids
            ]
            if not items:
                return []
            self._claimed_ids.update(item.message_id for item in items)
            await self._record("claim", InboxTarget.NEXT_STEP, items)
            return items

    async def commit(self, message_ids: Sequence[str]) -> None:
        committed = set(message_ids)
        if not committed:
            return
        async with self._lock:
            unknown = committed - self._claimed_ids
            if unknown:
                raise ValueError(
                    "Cannot commit unclaimed inbox ids: "
                    + ", ".join(sorted(unknown))
                )
            remaining = [
                item for item in self._items()
                if item.message_id not in committed
            ]
            self._persist(remaining)
            self._next_step = deque(
                item for item in self._next_step
                if item.message_id not in committed
            )
            self._next_turn = deque(
                item for item in self._next_turn
                if item.message_id not in committed
            )
            self._ids.difference_update(committed)
            self._claimed_ids.difference_update(committed)

    async def reconcile(
        self,
        message_ids: Sequence[str],
        committed_ids: set[str],
    ) -> None:
        """Commit durable claims and release the rest after an interrupted claim."""
        requested = set(message_ids)
        async with self._lock:
            active = requested & self._claimed_ids
            durable = active & committed_ids
            if durable:
                remaining = [
                    item for item in self._items()
                    if item.message_id not in durable
                ]
                self._persist(remaining)
                self._next_step = deque(
                    item for item in self._next_step
                    if item.message_id not in durable
                )
                self._next_turn = deque(
                    item for item in self._next_turn
                    if item.message_id not in durable
                )
                self._ids.difference_update(durable)
            self._claimed_ids.difference_update(active)

    async def edit(self, message_id: str, content: str) -> InboxInput:
        """Replace the text of one unclaimed input without changing its order."""
        if not content.strip():
            raise ValueError("Inbox input content cannot be empty")
        async with self._lock:
            current = self._pending_item(message_id)
            updated = replace(current, content=content)
            items = [
                updated if item.message_id == message_id else item
                for item in self._items()
            ]
            self._persist(items)
            queue = self._queue(current.target)
            queue[queue.index(current)] = updated
            await self._record("edit", updated.target, [updated])
            return updated

    async def remove(self, message_id: str) -> InboxInput:
        """Remove one unclaimed input from the durable and live queue."""
        async with self._lock:
            current = self._pending_item(message_id)
            items = [item for item in self._items() if item.message_id != message_id]
            self._persist(items)
            self._queue(current.target).remove(current)
            self._ids.remove(message_id)
            await self._record("remove", current.target, [current])
            return current

    async def retarget(
        self,
        message_id: str,
        target: InboxTarget | str,
    ) -> InboxInput:
        """Move one unclaimed input to the tail of another delivery target."""
        target = InboxTarget(target)
        async with self._lock:
            current = self._pending_item(message_id)
            if current.target is target:
                return current
            updated = replace(current, target=target)
            remaining = [item for item in self._items() if item.message_id != message_id]
            next_step = [item for item in remaining if item.target is InboxTarget.NEXT_STEP]
            next_turn = [item for item in remaining if item.target is InboxTarget.NEXT_TURN]
            (next_step if target is InboxTarget.NEXT_STEP else next_turn).append(updated)
            self._persist([*next_step, *next_turn])
            self._queue(current.target).remove(current)
            self._queue(target).append(updated)
            await self._record("retarget", target, [updated])
            return updated

    async def discard(self) -> list[InboxInput]:
        async with self._lock:
            items = [*self._next_step, *self._next_turn]
            if not items:
                return []
            self._persist([])
            self._next_step.clear()
            self._next_turn.clear()
            self._ids.clear()
            self._claimed_ids.clear()
            await self._record("discard", None, items)
            return items

    def _queue(self, target: InboxTarget) -> deque[InboxInput]:
        return self._next_turn if target is InboxTarget.NEXT_TURN else self._next_step

    def _items(self) -> list[InboxInput]:
        return [*self._next_step, *self._next_turn]

    def _pending_item(self, message_id: str) -> InboxInput:
        if message_id in self._claimed_ids:
            raise KeyError(message_id)
        item = next(
            (item for item in self._items() if item.message_id == message_id),
            None,
        )
        if item is None:
            raise KeyError(message_id)
        return item

    def _persist(self, items: Sequence[InboxInput]) -> None:
        if self._sink is not None:
            self._sink.replace(items)

    async def _record(
        self,
        operation: str,
        target: InboxTarget | None,
        items: list[InboxInput],
    ) -> None:
        if self._record_splice is None:
            return
        await self._record_splice({
            "type": "agent/inbox/spliced",
            "data": {
                "operation": operation,
                "target": target.value if target is not None else None,
                "message_ids": [item.message_id for item in items],
                "items": [
                    {
                        "message_id": item.message_id,
                        "content": item.content,
                        "target": item.target.value,
                        "source": item.source,
                        "images": [image.to_dict() for image in item.images],
                        "artifacts": [
                            artifact.to_dict() for artifact in item.artifacts
                        ],
                        "metadata": item.metadata,
                    }
                    for item in items
                ],
            },
        })

    def __len__(self) -> int:
        return sum(
            item.message_id not in self._claimed_ids
            for item in self._items()
        )

    @property
    def pending(self) -> list[InboxInput]:
        return [
            item for item in self._items()
            if item.message_id not in self._claimed_ids
        ]

__all__ = ["AgentInbox", "InboxInput", "InboxSink", "InboxTarget"]
