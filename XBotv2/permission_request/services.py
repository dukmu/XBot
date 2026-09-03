"""Public service contract for live permission approval."""

from __future__ import annotations

from typing import Protocol

from pydantic import JsonValue

from XBotv2.core.tools import ClientEvent


class ApprovalPort(Protocol):
    async def request(self, client_event: ClientEvent) -> dict[str, JsonValue]: ...


__all__ = ["ApprovalPort"]
