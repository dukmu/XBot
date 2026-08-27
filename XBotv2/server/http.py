"""FastAPI carrier for the XBot HTTP/SSE protocol.

The carrier owns only framework setup and protocol-wide error envelopes.
Business routes are contributed by plugins through the XCore ``http/route``
event.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from functools import partial
from typing import Any

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.types import ASGIApp, Message as ASGIMessage, Receive, Scope, Send

from XBotv2.core.errors import OperationError
from XBotv2.core.providers import BaseProvider
from XBotv2.core.runtime_logging import (
    DEFAULT_RUNTIME_LOG,
    RuntimeLog,
    push_log_context,
    reset_log_context,
)
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


class ApiLoggingMiddleware:
    """Pure-ASGI request logging without buffering streaming responses."""

    def __init__(self, app: ASGIApp, *, runtime_log: RuntimeLog) -> None:
        self._app = app
        self._log = runtime_log.bind("api")

    async def __call__(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
    ) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        exchange = _LoggedHttpExchange(
            app=self._app,
            scope=scope,
            receive=receive,
            send=send,
            runtime_log=self._log,
        )
        await exchange.run()


class _LoggedHttpExchange:
    """Explicit per-request state for :class:`ApiLoggingMiddleware`."""

    def __init__(
        self,
        *,
        app: ASGIApp,
        scope: Scope,
        receive: Receive,
        send: Send,
        runtime_log: RuntimeLog,
    ) -> None:
        self._app = app
        self._scope = scope
        self._receive = receive
        self._send = send
        self._log = runtime_log
        self._started = time.perf_counter()
        self._method = str(scope["method"])
        self._path = str(scope["path"])
        self._request_id = _request_id(scope)
        self._status: int | str = 500
        self._completed = False

    async def run(self) -> None:
        context_token = push_log_context(http_request_id=self._request_id)
        self._log.info(
            "api.request",
            method=self._method,
            path=self._path,
            http_request_id=self._request_id,
        )
        try:
            try:
                await self._app(self._scope, self._receive, self.send)
            except asyncio.CancelledError:
                self._finish("cancelled", level=logging.WARNING)
                raise
            except Exception as exc:
                self._fail(exc)
                raise
            if not self._completed:
                self._finish(self._status, incomplete=True)
        finally:
            reset_log_context(context_token)

    async def send(self, message: ASGIMessage) -> None:
        if message["type"] == "http.response.start":
            self._status = int(message["status"])
            headers = [
                header
                for header in message.get("headers", [])
                if header[0].lower() != b"x-request-id"
            ]
            headers.append((b"x-request-id", self._request_id.encode("latin-1")))
            message["headers"] = headers
        await self._send(message)
        if (
            message["type"] == "http.response.body"
            and not message.get("more_body", False)
        ):
            self._finish(self._status)

    def _finish(
        self,
        status: int | str,
        *,
        level: int = logging.INFO,
        incomplete: bool = False,
    ) -> None:
        if self._completed:
            return
        self._completed = True
        self._log.log(
            level,
            "api.response",
            method=self._method,
            path=self._path,
            http_request_id=self._request_id,
            status=status,
            duration_ms=round((time.perf_counter() - self._started) * 1000, 3),
            incomplete=incomplete,
        )

    def _fail(self, error: Exception) -> None:
        if self._completed:
            return
        self._completed = True
        self._log.error(
            "api.response",
            method=self._method,
            path=self._path,
            http_request_id=self._request_id,
            status=500,
            error_type=type(error).__name__,
            duration_ms=round((time.perf_counter() - self._started) * 1000, 3),
            incomplete=True,
        )


def _request_id(scope: Scope) -> str:
    for name, value in scope.get("headers", []):
        if name.lower() == b"x-request-id":
            return value.decode("latin-1")
    return uuid.uuid4().hex


def create_app(
    *,
    server_name: str = "xbotv2",
    runtime_log: RuntimeLog = DEFAULT_RUNTIME_LOG,
) -> FastAPI:
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
    app.add_middleware(ApiLoggingMiddleware, runtime_log=runtime_log)

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
        app.dependency_overrides[current_model_override] = partial(
            _fixed_model, llm
        )


async def _fixed_model(llm: BaseProvider) -> BaseProvider:
    return llm


__all__ = [
    "ModelOverride",
    "create_app",
    "current_model_override",
    "set_llm_override",
]
