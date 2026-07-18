"""Static Web client and same-origin API proxy."""

from __future__ import annotations

import mimetypes
from collections.abc import Mapping
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import Response, StreamingResponse
from starlette.background import BackgroundTask


_HOP_BY_HOP_HEADERS = {
    "connection",
    "content-length",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
}
_PROXY_METHODS = ["DELETE", "GET", "HEAD", "OPTIONS", "PATCH", "POST", "PUT"]


def create_web_app(
    static_root: Path,
    *,
    api_url: str,
    uds_path: str | None = None,
) -> FastAPI:
    """Create the local Web app backed by an HTTP or UDS XBot API."""
    static_root = static_root.resolve()
    index_path = static_root / "index.html"
    if not index_path.is_file() or not (static_root / "assets").is_dir():
        raise FileNotFoundError(f"Compiled Web client not found at {static_root}")

    app = FastAPI(
        title="XBot Web",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    @app.api_route("/api/{path:path}", methods=_PROXY_METHODS)
    async def proxy_api(path: str, request: Request) -> Response:
        transport = httpx.AsyncHTTPTransport(uds=uds_path) if uds_path else None
        client = httpx.AsyncClient(
            base_url=api_url,
            transport=transport,
            timeout=None,
        )
        upstream = client.build_request(
            request.method,
            f"/{path}",
            params=request.query_params,
            headers=_forward_headers(request.headers),
            content=await request.body(),
        )
        try:
            response = await client.send(upstream, stream=True)
        except BaseException:
            await client.aclose()
            raise
        headers = _response_headers(response.headers)
        if response.headers.get("content-type", "").startswith(
            "text/event-stream"
        ):
            return StreamingResponse(
                response.aiter_raw(),
                status_code=response.status_code,
                headers=headers,
                background=BackgroundTask(_close_upstream, response, client),
            )
        try:
            content = await response.aread()
        finally:
            await _close_upstream(response, client)
        return Response(
            content=content,
            status_code=response.status_code,
            headers=headers,
        )

    @app.get("/", include_in_schema=False)
    async def web_index() -> Response:
        return _static_response(index_path)

    @app.get("/{path:path}", include_in_schema=False)
    async def web_route(path: str) -> Response:
        candidate = (static_root / path).resolve()
        if candidate.is_relative_to(static_root) and candidate.is_file():
            return _static_response(candidate)
        return _static_response(index_path)

    return app


def _forward_headers(headers: Mapping[str, str]) -> dict[str, str]:
    return {
        name: value
        for name, value in headers.items()
        if name.lower() not in _HOP_BY_HOP_HEADERS | {"host"}
    }


def _response_headers(headers: httpx.Headers) -> dict[str, str]:
    return {
        name: value
        for name, value in headers.items()
        if name.lower() not in _HOP_BY_HOP_HEADERS | {"date", "server"}
    }


async def _close_upstream(
    response: httpx.Response,
    client: httpx.AsyncClient,
) -> None:
    await response.aclose()
    await client.aclose()


def _static_response(path: Path) -> Response:
    media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    cache_control = (
        "no-cache"
        if path.name == "index.html"
        else "public, max-age=31536000, immutable"
    )
    return Response(
        path.read_bytes(),
        media_type=media_type,
        headers={"Cache-Control": cache_control},
    )


__all__ = ["create_web_app"]
