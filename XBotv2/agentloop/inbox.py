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
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

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
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


SpliceRecorder = Callable[[dict[str, Any]], Awaitable[None]]
WakeDriver = Callable[[], None]


class AgentInbox:
    """Two FIFO input lists owned by one agent loop.

    Every mutation is recorded before the in-memory projection changes.  This
    preserves the ordering needed by a durable session-event recorder without
    coupling the inbox to a persistence implementation.
    """

    def __init__(
        self,
        *,
        record_splice: SpliceRecorder | None = None,
        wake_driver: WakeDriver | None = None,
    ) -> None:
        self._next_turn: deque[InboxInput] = deque()
        self._next_step: deque[InboxInput] = deque()
        self._ids: set[str] = set()
        self._lock = asyncio.Lock()
        self._record_splice = record_splice
        self._wake_driver = wake_driver

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
        artifacts: list[dict[str, Any]] | None = None,
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
            await self._record("insert", target, [item])
            self._queue(target).append(item)
            self._ids.add(item.message_id)
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
            items = list(self._next_step)
            if self._next_turn:
                items.append(self._next_turn[0])
            if not items:
                return []
            await self._record("claim", None, items)
            self._next_step.clear()
            if self._next_turn:
                self._next_turn.popleft()
            self._ids.difference_update(item.message_id for item in items)
            return items

    async def claim_step(self) -> list[InboxInput]:
        """Atomically claim only inputs targeted at the next loop step."""
        async with self._lock:
            items = list(self._next_step)
            if not items:
                return []
            await self._record("claim", InboxTarget.NEXT_STEP, items)
            self._next_step.clear()
            self._ids.difference_update(item.message_id for item in items)
            return items

    async def discard(self) -> list[InboxInput]:
        async with self._lock:
            items = [*self._next_step, *self._next_turn]
            if not items:
                return []
            await self._record("discard", None, items)
            self._next_step.clear()
            self._next_turn.clear()
            self._ids.clear()
            return items

    def restore(self, events: list[dict[str, Any]]) -> None:
        """Replay durable splice events into an empty live projection."""
        if self._ids or self._next_step or self._next_turn:
            raise RuntimeError("Inbox restore requires an empty projection")
        for event in events:
            data = dict(event.get("data") or {})
            operation = str(data.get("operation") or "")
            if operation == "insert":
                for raw in data.get("items") or []:
                    item = InboxInput(
                        content=str(raw.get("content") or ""),
                        target=InboxTarget(raw["target"]),
                        source=str(raw.get("source") or "user"),
                        message_id=str(raw["message_id"]),
                        images=[
                            ImageContent.from_dict(image)
                            for image in raw.get("images") or []
                        ],
                        artifacts=list(raw.get("artifacts") or []),
                        metadata=dict(raw.get("metadata") or {}),
                    )
                    if item.message_id in self._ids:
                        raise ValueError(
                            f"Duplicate restored inbox id: {item.message_id}"
                        )
                    self._queue(item.target).append(item)
                    self._ids.add(item.message_id)
            elif operation in {"claim", "discard"}:
                removed = set(data.get("message_ids") or [])
                self._next_step = deque(
                    item for item in self._next_step
                    if item.message_id not in removed
                )
                self._next_turn = deque(
                    item for item in self._next_turn
                    if item.message_id not in removed
                )
                self._ids.difference_update(removed)
            else:
                raise ValueError(f"Unknown inbox splice operation: {operation!r}")

    def _queue(self, target: InboxTarget) -> deque[InboxInput]:
        return self._next_turn if target is InboxTarget.NEXT_TURN else self._next_step

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
                        "artifacts": item.artifacts,
                        "metadata": item.metadata,
                    }
                    for item in items
                ] if operation == "insert" else [],
            },
        })

    def __len__(self) -> int:
        return len(self._next_step) + len(self._next_turn)

    @property
    def pending(self) -> list[InboxInput]:
        return [*self._next_step, *self._next_turn]

    @property
    def has_next_turn(self) -> bool:
        return bool(self._next_turn)


__all__ = ["AgentInbox", "InboxInput", "InboxTarget"]
