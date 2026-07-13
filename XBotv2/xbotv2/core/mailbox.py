"""Ephemeral per-session turn input queue with append-only diagnostics."""

from __future__ import annotations

import asyncio
import json
import uuid
from collections import deque
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal


MailboxKind = Literal["user_message", "general"]


@dataclass(frozen=True, slots=True)
class MailboxMessage:
    id: str
    kind: MailboxKind
    message: str | dict[str, Any]
    request_id: str = ""

    @classmethod
    def create(
        cls,
        kind: MailboxKind,
        message: str | dict[str, Any],
        *,
        request_id: str = "",
    ) -> "MailboxMessage":
        if kind not in {"user_message", "general"}:
            raise ValueError("mailbox kind must be user_message or general")
        return cls(
            id=f"mail-{uuid.uuid4().hex}",
            kind=kind,
            message=message,
            request_id=request_id,
        )


class SessionMailbox:
    """Runtime-only priority queue; its audit log is never replayed."""

    def __init__(self, audit_path: Path) -> None:
        self._audit_path = audit_path
        self._user: deque[MailboxMessage] = deque()
        self._general: deque[MailboxMessage] = deque()
        self._condition = asyncio.Condition()
        self._closed = False

    async def put(self, item: MailboxMessage) -> None:
        async with self._condition:
            if self._closed:
                raise RuntimeError("mailbox is closed")
            queue = self._user if item.kind == "user_message" else self._general
            queue.append(item)
            self._audit("enqueued", item)
            self._condition.notify()

    async def get(self) -> MailboxMessage | None:
        async with self._condition:
            await self._condition.wait_for(
                lambda: self._closed or bool(self._user or self._general)
            )
            if self._user:
                item = self._user.popleft()
            elif self._general:
                item = self._general.popleft()
            else:
                return None
            self._audit("dequeued", item)
            return item

    async def close(self, reason: str = "session_closed") -> list[MailboxMessage]:
        async with self._condition:
            if self._closed:
                return []
            self._closed = True
            dropped = [*self._user, *self._general]
            self._user.clear()
            self._general.clear()
            for item in dropped:
                self._audit("dropped", item, reason=reason)
            self._condition.notify_all()
            return dropped

    def delivered(self, item: MailboxMessage) -> None:
        self._audit("delivered", item)

    def failed(self, item: MailboxMessage, error: BaseException) -> None:
        self._audit(
            "failed",
            item,
            error={"type": type(error).__name__, "message": str(error)},
        )

    def dropped(self, item: MailboxMessage, reason: str) -> None:
        self._audit("dropped", item, reason=reason)

    @property
    def size(self) -> int:
        return len(self._user) + len(self._general)

    @property
    def next_kind(self) -> MailboxKind | None:
        if self._user:
            return "user_message"
        if self._general:
            return "general"
        return None

    def _audit(self, event: str, item: MailboxMessage, **details: Any) -> None:
        record = {
            "time": datetime.now(UTC).isoformat(),
            "event": event,
            "item": asdict(item),
            **details,
        }
        self._audit_path.parent.mkdir(parents=True, exist_ok=True)
        with self._audit_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")


__all__ = ["MailboxKind", "MailboxMessage", "SessionMailbox"]
