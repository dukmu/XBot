"""Wire-level HTTP framing helpers for the server route modules.

This module is pure protocol: error envelopes and SSE framing built from the
wire DTOs and SSE encoder only. It imports no application or plugin logic.
Capability response builders live in their owning plugin ``protocol`` modules.
"""

from __future__ import annotations

import logging
from typing import Any

from XBotv2.protocol.models import ErrorResponse, server_event
from XBotv2.protocol.sse import encode_server_event
from XBotv2.protocol.version import PROTOCOL_VERSION

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


class HttpServerError(Exception):
    """Domain error with an HTTP status hint."""

    def __init__(
        self,
        code: str,
        message: str,
        status: int = 400,
        *,
        details: dict[str, Any] | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.details = details or {}
        self.retryable = retryable


def _error_payload(
    code: str,
    message: str,
    *,
    details: dict[str, Any] | None = None,
    retryable: bool = False,
) -> dict[str, Any]:
    return ErrorResponse(
        code=code,
        message=message,
        details=details or {},
        retryable=retryable,
    ).model_dump()


def _format_sse(
    *,
    event: dict[str, Any],
    seq: int,
    session_id: str = "",
    thread_id: str = "agent",
    request_id: str = "",
) -> bytes:
    """Format a single SSE frame.

    Per §10.5.4: ``event: <type>`` and ``data: <json>`` on separate
    lines, with a single ``id: <seq>`` line. The ``type`` and the
    SSE ``event`` field share the same name so consumers can use
    either listener style.
    """

    payload_event = server_event(
        protocol_version=PROTOCOL_VERSION,
        session_id=session_id,
        thread_id=thread_id,
        request_id=request_id,
        sequence=seq,
        type=str(event.get("type", "message") or "message"),
        data=dict(event.get("data") or {}),
    )
    return encode_server_event(payload_event)
