"""Public service contract for live permission approval."""

from __future__ import annotations

from typing import Any, Protocol


class ApprovalPort(Protocol):
    async def request(self, client_event: dict[str, Any]) -> dict[str, Any]: ...


__all__ = ["ApprovalPort"]
