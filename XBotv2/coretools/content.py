"""Provider-visible content tool for non-text model input."""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

from XBotv2.core.messages import ImageContent
from XBotv2.core.tools import Tool, ToolResult
from XBotv2.sandbox.filesystem_ops import execute


MAX_CONTENT_BYTES = 25 * 1024 * 1024
SUPPORTED_IMAGE_TYPES = frozenset({
    "image/gif",
    "image/jpeg",
    "image/png",
    "image/webp",
})
_READ_BYTES = "read_bytes"


class ContentError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


async def content_read(
    path: str | None = None,
    url: str | None = None,
    data: str | None = None,
    media_type: str | None = None,
    *,
    sandbox=None,
) -> ToolResult:
    """Read one image and make it visible to the model.

    Exactly one of ``path``, ``url``, or ``data`` must be provided.

    - ``path``: workspace-relative, absolute approved, or ``session/`` path.
    - ``url``: an ``http`` or ``https`` image URL.
    - ``data``: base64 image bytes, optionally as a ``data:image/*;base64,`` URL.
    - ``media_type``: required when ``data`` cannot be inferred and optional
      as an override for ``path`` or ``url``.

    Supported types are GIF, JPEG, PNG, and WebP. The bytes are stored under
    ``session/artifacts/media/`` and returned as an image part. Files larger
    than 25 MiB, network access with sandbox networking disabled, and
    unsupported content fail with a structured Tool error.
    """
    sources = [value for value in (path, url, data) if value]
    if len(sources) != 1:
        return ToolResult.failure(
            "invalid_content_source",
            "content_read requires exactly one of path, url, or data",
        )

    try:
        if url is not None:
            payload, media_type, metadata = await _read_url(url, media_type, sandbox)
        elif path is not None:
            payload, media_type, metadata = await _read_path(path, media_type, sandbox)
        else:
            payload, media_type, metadata = _decode_data(str(data or ""), media_type)
        if len(payload) > MAX_CONTENT_BYTES:
            return ToolResult.failure(
                "content_too_large",
                f"Content exceeds {MAX_CONTENT_BYTES} bytes",
            )
        selected = _image_type(payload, media_type)
        image = _store_image(payload, selected, sandbox)
    except ContentError as exc:
        return ToolResult.failure(exc.code, exc.message)

    result_data: dict[str, Any] = {
        "media_type": selected,
        "size_bytes": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
    }
    result_data.update(metadata)
    return ToolResult.success(
        f"Image content loaded: {selected} ({len(payload)} bytes)",
        data=result_data,
        images=(image,),
    )


async def _read_path(
    path: str,
    media_type: str | None,
    sandbox: Any,
) -> tuple[bytes, str | None, dict[str, Any]]:
    args = {"path": path}
    if sandbox is not None:
        resolved = sandbox.resolve_filesystem_args(_READ_BYTES, args)
        if sandbox.enabled:
            raw = await sandbox.filesystem(_READ_BYTES, args)
            data = _parse_json(raw)
        else:
            data = execute(_READ_BYTES, resolved)
    else:
        data = execute(
            _READ_BYTES,
            {"path": str(Path(path).expanduser().absolute())},
        )
    if not data.get("ok"):
        error = data.get("error") or {}
        raise ContentError(
            str(error.get("code") or "content_read_failed"),
            str(error.get("message") or "Unable to read path"),
        )
    size = int(data.get("size_bytes") or 0)
    if size > MAX_CONTENT_BYTES:
        raise ContentError("content_too_large", f"Content exceeds {MAX_CONTENT_BYTES} bytes")
    encoded = data.get("base64")
    if not isinstance(encoded, str):
        raise ContentError("invalid_result", "Filesystem backend returned no base64 content")
    return (
        base64.b64decode(encoded),
        media_type or str(data.get("media_type") or ""),
        {"path": path},
    )


async def _read_url(
    url: str,
    media_type: str | None,
    sandbox: Any,
) -> tuple[bytes, str | None, dict[str, Any]]:
    if sandbox is not None and not sandbox.network:
        raise ContentError("network_disabled", "Sandbox network access is disabled")
    parsed = urlsplit(url.strip())
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
    ):
        raise ContentError("invalid_url", "URL must be an http or https image URL")

    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
        try:
            async with client.stream(
                "GET",
                url.strip(),
                headers={
                    "User-Agent": "XBotv2/0.2 content tool",
                    "Accept": "image/*",
                },
            ) as response:
                if response.status_code >= 400:
                    raise ContentError(
                        "url_error",
                        f"URL returned HTTP {response.status_code}",
                    )
                chunks: list[bytes] = []
                size = 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > MAX_CONTENT_BYTES:
                        raise ContentError(
                            "content_too_large",
                            f"Content exceeds {MAX_CONTENT_BYTES} bytes",
                        )
                    chunks.append(chunk)
                content_type = response.headers.get(
                    "content-type",
                    "",
                ).split(";", 1)[0].strip().lower() or None
        except ContentError:
            raise
        except Exception as exc:
            raise ContentError("url_error", f"URL fetch failed: {exc}") from exc
    return b"".join(chunks), media_type or content_type, {"url": url.strip()}


def _decode_data(
    value: str,
    media_type: str | None,
) -> tuple[bytes, str | None, dict[str, Any]]:
    raw = value.strip()
    if raw.startswith("data:"):
        header, separator, encoded = raw.partition(",")
        if not separator or not header.endswith(";base64"):
            raise ContentError(
                "invalid_data_url",
                "data URLs must use base64 encoding",
            )
        header_media_type = header[5:-len(";base64")] or ""
        media_type = media_type or header_media_type
        encoded = "".join(encoded.split())
        source = "data_url"
    else:
        encoded = "".join(raw.split())
        source = "base64"
    try:
        payload = base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError) as exc:
        raise ContentError("invalid_base64", "data is not valid base64") from exc
    if not payload:
        raise ContentError("empty_content", "data contains no image bytes")
    return payload, media_type, {"source": source}


def _image_type(payload: bytes, media_type: str | None) -> str:
    inferred = _infer_image_type(payload)
    if inferred is None:
        hint = " Provide media_type for base64 input." if media_type is None else ""
        raise ContentError(
            "unsupported_content",
            f"Unsupported or unrecognized image content{hint}",
        )
    selected = (media_type or inferred).strip().lower()
    if selected not in SUPPORTED_IMAGE_TYPES:
        raise ContentError(
            "unsupported_content",
            f"Unsupported image type {selected}",
        )
    if selected != inferred:
        raise ContentError(
            "content_type_mismatch",
            f"Content is {inferred}, not {selected}",
        )
    return selected


def _infer_image_type(payload: bytes) -> str | None:
    if payload.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if payload.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if payload.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if payload.startswith(b"RIFF") and payload[8:12] == b"WEBP":
        return "image/webp"
    return None


def _store_image(
    payload: bytes,
    media_type: str,
    sandbox: Any,
) -> ImageContent:
    session_root = getattr(sandbox, "session_root", None) if sandbox is not None else None
    if session_root is None:
        raise ContentError(
            "content_storage_unavailable",
            "Session media storage is not available",
        )
    root = Path(session_root)
    digest = hashlib.sha256(payload).hexdigest()
    target = root / "artifacts" / "media" / digest
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        target.write_bytes(payload)
    return ImageContent(
        path=target.relative_to(root).as_posix(),
        media_type=media_type,
        size=len(payload),
    )


def _parse_json(value: str) -> dict[str, Any]:
    try:
        data = json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return {"ok": False, "error": {"code": "invalid_result", "message": "Invalid JSON"}}
    return data if isinstance(data, dict) else {
        "ok": False,
        "error": {"code": "invalid_result", "message": "Non-object JSON"},
    }


content_read_tool = Tool.from_function(content_read)


__all__ = ["content_read", "content_read_tool"]
