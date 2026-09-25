"""Wire-level HTTP response helpers for the server route modules.

This module is pure protocol: error envelopes and streaming response headers.
It imports no application or plugin logic.
Capability response builders live in their owning plugin ``protocol`` modules.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from pydantic import JsonValue
from fastapi.responses import StreamingResponse

from XBotv2.protocol.models import ErrorResponse

logger = logging.getLogger("xbotv2.api")

_SSE_RESPONSE = {
    200: {
        "description": "Server-Sent Events stream",
        "content": {
            "text/event-stream": {
                "schema": {"type": "string"},
            },
        },
    },
}


def _sse_response(events: AsyncIterator[bytes]) -> StreamingResponse:
    """Build the standard unbuffered response shared by SSE routes."""
    return StreamingResponse(
        events,
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


class HttpServerError(Exception):
    """Domain error with an HTTP status hint."""

    def __init__(
        self,
        code: str,
        message: str,
        status: int = 400,
        *,
        details: dict[str, JsonValue] | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.details = details or {}
        self.retryable = retryable


def error_payload(
    code: str,
    message: str,
    *,
    details: dict[str, JsonValue] | None = None,
    retryable: bool = False,
) -> dict[str, JsonValue]:
    return ErrorResponse(
        code=code,
        message=message,
        details=details or {},
        retryable=retryable,
    ).model_dump()
