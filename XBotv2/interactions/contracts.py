"""Public service contract for live client interactions."""

from __future__ import annotations

from typing import Protocol

from pydantic import JsonValue


class InteractionsPort(Protocol):
    async def request_user_input(
        self,
        question: str,
        *,
        options: list[dict[str, str]] | None = None,
        source: str = "interaction",
        timeout_seconds: float | None = None,
        tool_call_id: str = "",
    ) -> dict[str, JsonValue]: ...


__all__ = ["InteractionsPort"]
