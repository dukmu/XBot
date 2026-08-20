"""FastAPI carrier for the XBot HTTP/SSE protocol.

The carrier owns only framework setup and protocol-wide error envelopes.
Business routes are contributed by plugins through the XCore ``http/route``
event.
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from XBotv2.core.errors import OperationError
from XBotv2.core.providers import BaseProvider
from XBotv2.protocol.http_util import HttpServerError, _error_payload
from XBotv2.protocol import ErrorResponse
from XBotv2.protocol.version import PROTOCOL_VERSION
from XBotv2.server.contracts import (
    ModelOverride,
    current_model_override,
)

# Preserve the established helper import surface.
from XBotv2.protocol.http_util import (  # noqa: F401
    _SSE_RESPONSE,
    _format_sse,
)


def create_app(*, server_name: str = "xbotv2") -> FastAPI:
    """Create an empty protocol carrier; plugins contribute every route."""
    error_responses = {
        status: {"model": ErrorResponse, "description": description}
        for status, description in {
            400: "Invalid request",
            404: "Resource not found",
            409: "Resource state conflict",
            410: "Interaction no longer pending",
            422: "Request schema validation failed",
            426: "Unsupported protocol version",
            500: "Server error",
        }.items()
    }
    app = FastAPI(
        title=server_name,
        version=PROTOCOL_VERSION,
        responses=error_responses,
    )

    @app.exception_handler(HttpServerError)
    async def _on_http_error(_: Request, exc: HttpServerError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status,
            content=_error_payload(
                exc.code,
                exc.message,
                details=exc.details,
                retryable=exc.retryable,
            ),
        )

    @app.exception_handler(OperationError)
    async def _on_operation_error(
        _: Request, exc: OperationError
    ) -> JSONResponse:
        if exc.code.endswith("_not_found"):
            status = 404
        elif exc.code in {
            "event_stream_connected",
            "parent_thread_not_active",
            "task_not_background",
            "thread_busy",
        }:
            status = 409
        else:
            status = 400
        return JSONResponse(
            status_code=status,
            content=_error_payload(
                exc.code,
                exc.message,
                retryable=exc.retryable,
            ),
        )

    @app.exception_handler(RequestValidationError)
    async def _on_validation_error(
        _: Request, exc: RequestValidationError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=400,
            content=_error_payload(
                "invalid_request",
                "Request does not match the protocol schema",
                details={
                    "errors": jsonable_encoder(
                        exc.errors(),
                        custom_encoder={Exception: str},
                    )
                },
            ),
        )

    return app


def set_llm_override(app: FastAPI, llm: BaseProvider | None) -> None:
    """Override the model provider through FastAPI's dependency mechanism."""
    if llm is None:
        app.dependency_overrides.pop(current_model_override, None)
    else:
        app.dependency_overrides[current_model_override] = lambda: llm


__all__ = [
    "ModelOverride",
    "create_app",
    "current_model_override",
    "set_llm_override",
]
