"""Merged filesystem tools: ``read``, ``edit``, ``path``, ``search``.

The four model-facing tools replace the previous twelve granular tools:

- ``read``: UTF-8 text, binary bytes, stat metadata, image open, or directory
  listing (``mode``).
- ``edit``: write a whole file, replace exact text, or apply a unified diff
  (``mode``).
- ``path``: move, copy, delete, or create a path (``operation``).
- ``search``: content regex search or name glob (``mode``).

The sandbox backend keeps its canonical per-operation dispatch; each merged
tool selects the operation from its ``mode`` / ``operation`` argument, so
permissions resolve per call.  Module-level helper functions remain for tests
and backend reuse.
"""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit
from weakref import WeakKeyDictionary

import httpx

from XBotv2.core.messages import ImageContent
from XBotv2.core.tools import Tool, ToolResult
from XBotv2.core.filesystem.operations import PATH_ACCESS, execute

_FILE_VERSIONS: WeakKeyDictionary[Any, dict[str, str]] = WeakKeyDictionary()


# ----------------------------------------------------------------------
# read: utf8 / binary / stat / image / list
# ----------------------------------------------------------------------


async def read(
    path: str,
    mode: Literal["utf8", "binary", "stat", "media", "list"] = "utf8",
    offset: int = 0,
    limit: int = 2000,
    char_offset: int = 0,
    max_chars: int = 12000,
    line_numbers: bool = False,
    url: str | None = None,
    data: str | None = None,
    media_type: str | None = None,
    recursive: bool = False,
    max_entries: int = 500,
    include_hidden: bool = True,
    *,
    sandbox=None,
) -> ToolResult:
    """Read file content, bytes, metadata, an image, or a directory listing.

    ``mode`` selects the operation:

    - ``utf8`` (default): bounded UTF-8 text read with line/character limits.
      Non-UTF-8 files return metadata (MIME, size, SHA-256, image dimensions)
      instead of binary content. Continue truncated reads from the returned
      next offsets. The ``session/`` virtual path is read-only.
    - ``binary``: return raw bytes as base64 with metadata; the model sees the
      encoding sidecar, not the payload.
    - ``stat``: return metadata for a file, directory, or symlink without
      reading content.
    - ``media``: open one media item by its type (``path``, ``url``, or
      base64 ``data``) and make it visible to the model as a native part.
      Images are supported today.
    - ``list``: list a directory's entries with bounded metadata; recursive
      traversal stops at ``max_entries`` and never follows symlinks.

    Args:
        path: Workspace-relative, absolute approved, or ``session/`` path.
        mode: Operation to perform.
        offset: Zero-based first line (utf8).
        limit: Maximum lines (utf8).
        char_offset: Character offset within the first selected line (utf8).
        max_chars: Maximum raw characters returned (utf8).
        line_numbers: Prefix displayed text with one-based line numbers (utf8).
        url: Media URL (media).
        data: Base64 media bytes or ``data:*;base64,`` URL (media).
        media_type: Required when ``data`` cannot be inferred (media).
        recursive: Include descendants (list).
        max_entries: Maximum returned entries (list).
        include_hidden: Include names beginning with a dot (list).
    """
    if mode == "utf8":
        return await read_file(
            path,
            offset=offset,
            limit=limit,
            char_offset=char_offset,
            max_chars=max_chars,
            line_numbers=line_numbers,
            sandbox=sandbox,
        )
    if mode == "binary":
        return await read_bytes_file(path, sandbox=sandbox)
    if mode == "stat":
        return await stat_path(path, sandbox=sandbox)
    if mode == "media":
        return await _read_media(
            path=path,
            url=url,
            data=data,
            media_type=media_type,
            sandbox=sandbox,
        )
    if mode == "list":
        return await list_files(
            path=path,
            recursive=recursive,
            max_entries=max_entries,
            include_hidden=include_hidden,
            sandbox=sandbox,
        )
    return ToolResult.failure("invalid_mode", f"Unknown read mode: {mode}")


MAX_CONTENT_BYTES = 25 * 1024 * 1024
SUPPORTED_IMAGE_TYPES = frozenset({
    "image/gif",
    "image/jpeg",
    "image/png",
    "image/webp",
})


class _ImageError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


async def _read_media(
    path: str,
    url: str | None,
    data: str | None,
    media_type: str | None,
    sandbox: Any,
) -> ToolResult:
    """Open one media item by type and make it visible to the model.

    ``read(mode=media)`` is the single model-facing content tool. Exactly one
    of ``path``, ``url``, or ``data`` is required; images (GIF, JPEG, PNG,
    WebP) are supported today. Bytes are stored under
    ``session/artifacts/media/`` and returned as a model-visible part.
    """
    sources = [value for value in (path, url, data) if value]
    if len(sources) != 1:
        return ToolResult.failure(
            "invalid_content_source",
            "read media mode requires exactly one of path, url, or data",
        )
    try:
        if url is not None:
            payload, media_type, metadata = await _read_image_url(
                url, media_type, sandbox
            )
        elif path:
            payload, media_type, metadata = await _read_image_path(
                path, media_type, sandbox
            )
        else:
            payload, media_type, metadata = _decode_image_data(
                str(data or ""), media_type
            )
        if len(payload) > MAX_CONTENT_BYTES:
            return ToolResult.failure(
                "content_too_large",
                f"Content exceeds {MAX_CONTENT_BYTES} bytes",
            )
        selected = _image_type(payload, media_type)
        image = _store_image(payload, selected, sandbox)
    except _ImageError as exc:
        return ToolResult.failure(exc.code, exc.message)

    result_data: dict[str, Any] = {
        "media_type": selected,
        "size_bytes": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
    }
    result_data.update(metadata)
    return ToolResult.success(
        f"Image content loaded: {selected} ({len(payload)} bytes)",
        images=(image,),
    )


async def _read_image_path(
    path: str,
    media_type: str | None,
    sandbox: Any,
) -> tuple[bytes, str | None, dict[str, Any]]:
    data = await _operation("read_bytes", {"path": path}, sandbox)
    if not data.get("ok"):
        error = data.get("error") or {}
        raise _ImageError(
            str(error.get("code") or "read_failed"),
            str(error.get("message") or "Unable to read path"),
        )
    size = int(data.get("size_bytes") or 0)
    if size > MAX_CONTENT_BYTES:
        raise _ImageError(
            "content_too_large",
            f"Content exceeds {MAX_CONTENT_BYTES} bytes",
        )
    encoded = data.get("base64")
    if not isinstance(encoded, str):
        raise _ImageError(
            "invalid_result",
            "Filesystem backend returned no base64 content",
        )
    return (
        base64.b64decode(encoded),
        media_type or str(data.get("media_type") or ""),
        {"path": path},
    )


async def _read_image_url(
    url: str,
    media_type: str | None,
    sandbox: Any,
) -> tuple[bytes, str | None, dict[str, Any]]:
    if sandbox is not None and not sandbox.network:
        raise _ImageError(
            "network_disabled",
            "Sandbox network access is disabled",
        )
    parsed = urlsplit(url.strip())
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
    ):
        raise _ImageError(
            "invalid_url",
            "URL must be an http or https image URL",
        )
    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
        try:
            async with client.stream(
                "GET",
                url.strip(),
                headers={
                    "User-Agent": "XBotv2/0.2 read tool",
                    "Accept": "image/*",
                },
            ) as response:
                if response.status_code >= 400:
                    raise _ImageError(
                        "url_error",
                        f"URL returned HTTP {response.status_code}",
                    )
                chunks: list[bytes] = []
                size = 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > MAX_CONTENT_BYTES:
                        raise _ImageError(
                            "content_too_large",
                            f"Content exceeds {MAX_CONTENT_BYTES} bytes",
                        )
                    chunks.append(chunk)
                content_type = response.headers.get(
                    "content-type",
                    "",
                ).split(";", 1)[0].strip().lower() or None
        except _ImageError:
            raise
        except Exception as exc:
            raise _ImageError("url_error", f"URL fetch failed: {exc}") from exc
    return b"".join(chunks), media_type or content_type, {"url": url.strip()}


def _decode_image_data(
    value: str,
    media_type: str | None,
) -> tuple[bytes, str | None, dict[str, Any]]:
    raw = value.strip()
    if raw.startswith("data:"):
        header, separator, encoded = raw.partition(",")
        if not separator or not header.endswith(";base64"):
            raise _ImageError(
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
        raise _ImageError("invalid_base64", "data is not valid base64") from exc
    if not payload:
        raise _ImageError("empty_content", "data contains no image bytes")
    return payload, media_type, {"source": source}


def _image_type(payload: bytes, media_type: str | None) -> str:
    inferred = _infer_image_type(payload)
    if inferred is None:
        hint = (
            " Provide media_type for base64 input."
            if media_type is None
            else ""
        )
        raise _ImageError(
            "unsupported_content",
            f"Unsupported or unrecognized image content{hint}",
        )
    selected = (media_type or inferred).strip().lower()
    if selected not in SUPPORTED_IMAGE_TYPES:
        raise _ImageError(
            "unsupported_content",
            f"Unsupported image type {selected}",
        )
    if selected != inferred:
        raise _ImageError(
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
    session_root = (
        getattr(sandbox, "session_root", None) if sandbox is not None else None
    )
    if session_root is None:
        raise _ImageError(
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


async def read_file(
    path: str,
    offset: int = 0,
    limit: int = 2000,
    char_offset: int = 0,
    max_chars: int = 12000,
    line_numbers: bool = False,
    *,
    sandbox=None,
) -> ToolResult:
    """Read a bounded UTF-8 text range or return non-text metadata.

    Text reads are limited by both line count and character count. If the
    result is truncated, continue from the returned next offsets before
    relying on omitted content. Non-UTF-8 files return metadata instead of
    binary content, including MIME type, size, SHA-256, and recognized image
    dimensions. Use ``read`` with ``mode="media"`` to send recognized image
    files to the model. The ``session/`` virtual path is read-only.

    Args:
        path: Workspace-relative, absolute approved, or ``session/`` file path.
        offset: Zero-based first line.
        limit: Maximum lines; must be at least one.
        char_offset: Character offset within the first selected line.
        max_chars: Maximum raw characters returned; must be at least one.
        line_numbers: Prefix displayed text with one-based line numbers.
    """
    data = await _operation(
        "read",
        {
            "path": path,
            "offset": offset,
            "limit": limit,
            "char_offset": char_offset,
            "max_chars": max_chars,
        },
        sandbox,
    )
    if not data.get("ok"):
        return _failure(data)
    content = str(data.pop("content", ""))
    if data.get("changed_since_last_observation"):
        content = (
            "File changed since the previous read. The content and metadata "
            f"below are current.\n{content}"
        )
    data["requested_path"] = path
    if not data.get("is_text", True):
        image = data.get("image") or {}
        dimensions = (
            f", {image.get('width')}x{image.get('height')} {image.get('format')}"
            if image else ""
        )
        return ToolResult.success(
            f"Non-text file: {path} ({data.get('media_type')}, "
            f"{data.get('size_bytes')} bytes{dimensions}, sha256={data.get('sha256')})"
        )
    elif line_numbers:
        content = _with_line_numbers(content, offset + 1)
    return ToolResult.success(content)


async def read_bytes_file(path: str, *, sandbox=None) -> ToolResult:
    """Read raw file bytes as base64 with metadata (no model-visible payload).

    Args:
        path: Workspace-relative, absolute approved, or ``session/`` file path.
    """
    data = await _operation("read_bytes", {"path": path}, sandbox)
    if not data.get("ok"):
        return _failure(data)
    payload = str(data.pop("base64", ""))
    data["requested_path"] = path
    return ToolResult.success(
        f"Binary file: {path} ({data.get('size_bytes')} bytes, "
        f"sha256={data.get('sha256')}, base64 in data)"
    )


async def stat_path(path: str, *, sandbox=None) -> ToolResult:
    """Return metadata for a file, directory, or symbolic link.

    Regular files include size, mtime, SHA-256, UTF-8 status, inferred MIME
    type, extension, and recognized image dimensions. No file content is
    returned.

    Args:
        path: Workspace-relative, absolute approved, or ``session/`` path.
    """
    return await _structured_operation("stat", {"path": path}, sandbox)


async def list_files(
    path: str = ".",
    recursive: bool = False,
    max_entries: int = 500,
    include_hidden: bool = True,
    *,
    sandbox=None,
) -> ToolResult:
    """List directory entries with bounded metadata.

    Results distinguish files, directories, and symbolic links. Recursive
    traversal does not follow symlinks and stops once ``max_entries`` is
    reached instead of scanning the complete tree.

    Args:
        path: Workspace-relative, absolute approved, or ``session/`` directory.
        recursive: Include descendants when true.
        max_entries: Maximum returned entries; must be at least one.
        include_hidden: Include names beginning with a dot.
    """
    data = await _operation(
        "list",
        {
            "path": path,
            "recursive": recursive,
            "max_entries": max_entries,
            "include_hidden": include_hidden,
        },
        sandbox,
    )
    return _data_result(data)


# ----------------------------------------------------------------------
# edit: write / replace / patch
# ----------------------------------------------------------------------


async def edit(
    path: str,
    mode: Literal["write", "replace", "patch"] = "replace",
    content: str | None = None,
    old_text: str | None = None,
    new_text: str | None = None,
    replace_all: bool = False,
    patch: str | None = None,
    *,
    sandbox=None,
) -> ToolResult:
    """Edit one UTF-8 file: write whole content, replace text, or apply a diff.

    ``mode`` selects the operation:

    - ``write``: atomically create or completely replace the file with
      ``content``. Parent directories are created; existing non-UTF-8 files
      are rejected.
    - ``replace`` (default): atomically replace exact ``old_text`` with
      ``new_text``; fails when the match is absent or ambiguous unless
      ``replace_all`` is set.
    - ``patch``: apply a validated single-file unified diff in ``patch``.

    A file observed earlier in this runtime is protected against external
    changes by its last runtime snapshot; external modification invalidates
    that snapshot and returns ``content_changed``. Reading the file again
    refreshes it.

    Args:
        path: Existing text file to edit.
        mode: Operation to perform.
        content: Complete UTF-8 file content (write).
        old_text: Exact non-empty text expected in the file (replace).
        new_text: Replacement text (replace).
        replace_all: Replace every occurrence instead of requiring one match
            (replace).
        patch: Complete unified diff with file headers and at least one hunk
            (patch).
    """
    if mode == "write":
        if content is None:
            return ToolResult.failure(
                "invalid_arguments", "write mode requires content"
            )
        return await write_file(path, content, sandbox=sandbox)
    if mode == "replace":
        if old_text is None or new_text is None:
            return ToolResult.failure(
                "invalid_arguments", "replace mode requires old_text and new_text"
            )
        return await edit_file(
            path,
            old_text,
            new_text,
            replace_all=replace_all,
            sandbox=sandbox,
        )
    if mode == "patch":
        if patch is None:
            return ToolResult.failure(
                "invalid_arguments", "patch mode requires a patch"
            )
        return await patch_file(path, patch, sandbox=sandbox)
    return ToolResult.failure("invalid_mode", f"Unknown edit mode: {mode}")


async def write_file(
    path: str,
    content: str,
    *,
    sandbox=None,
) -> ToolResult:
    """Atomically create or completely replace one UTF-8 text file.

    ``content`` is the entire final file, not a fragment or patch. Parent
    directories are created. Existing non-UTF-8 files are rejected, and a file
    observed earlier in this runtime is protected against external changes.
    Use ``edit`` with ``mode="replace"`` for an exact replacement or
    ``mode="patch"`` for a unified diff.

    Args:
        path: Destination path relative to the workspace unless explicitly approved.
        content: Complete UTF-8 file content.
    """
    return await _structured_operation(
        "write",
        {"path": path, "content": content},
        sandbox,
    )


async def edit_file(
    path: str,
    old_text: str,
    new_text: str,
    replace_all: bool = False,
    *,
    sandbox=None,
) -> ToolResult:
    """Atomically replace exact text in an existing UTF-8 file.

    Read the relevant range first and provide enough surrounding text to select
    one occurrence. The edit fails when ``old_text`` is absent or ambiguous;
    set ``replace_all`` only when every occurrence should change. A file
    observed earlier is protected by its last runtime snapshot. External
    modification invalidates that snapshot and returns ``content_changed``;
    reading the file again refreshes it.

    Args:
        path: Existing text file.
        old_text: Exact non-empty text expected in the file.
        new_text: Replacement text.
        replace_all: Replace every occurrence instead of requiring one match.
    """
    return await _structured_operation(
        "edit",
        {
            "path": path,
            "old_text": old_text,
            "new_text": new_text,
            "replace_all": replace_all,
        },
        sandbox,
    )


async def patch_file(
    path: str,
    patch: str,
    *,
    sandbox=None,
) -> ToolResult:
    """Apply a validated unified diff to one UTF-8 file.

    The diff must match current content and target only ``path``. The system
    ``patch`` implementation performs a dry run before applying any hunk. A
    file observed earlier is protected against external changes by its last
    runtime snapshot.

    Args:
        path: File created, updated, or deleted by the patch.
        patch: Complete unified diff with file headers and at least one hunk.
    """
    return await _structured_operation(
        "patch",
        {"path": path, "patch": patch},
        sandbox,
    )


# ----------------------------------------------------------------------
# path: move / copy / delete / mkdir
# ----------------------------------------------------------------------


async def path(
    operation: Literal["move", "copy", "delete", "mkdir"] = "mkdir",
    path: str = "",
    source: str | None = None,
    destination: str | None = None,
    overwrite: bool = False,
    recursive: bool = False,
    parents: bool = True,
    *,
    sandbox=None,
) -> ToolResult:
    """Manage filesystem paths: move, copy, delete, or create a directory.

    ``operation`` selects the action:

    - ``move``: move or rename ``source`` to ``destination``.
    - ``copy``: copy ``source`` to ``destination`` without decoding content.
    - ``delete``: delete ``path`` (non-empty directories require
      ``recursive=true``).
    - ``mkdir``: create an empty directory at ``path``.

    Args:
        operation: Action to perform.
        path: Target path for delete/mkdir.
        source: Existing source path for move/copy.
        destination: New path for move/copy; its parent is created automatically.
        overwrite: Remove an existing destination before moving/copying.
        recursive: Recursively delete a non-empty directory (delete).
        parents: Create missing parent directories (mkdir).
    """
    if operation == "move":
        if source is None or destination is None:
            return ToolResult.failure(
                "invalid_arguments", "move requires source and destination"
            )
        return await move_path(source, destination, overwrite=overwrite, sandbox=sandbox)
    if operation == "copy":
        if source is None or destination is None:
            return ToolResult.failure(
                "invalid_arguments", "copy requires source and destination"
            )
        return await copy_path(source, destination, overwrite=overwrite, sandbox=sandbox)
    if operation == "delete":
        if not path:
            return ToolResult.failure("invalid_arguments", "delete requires path")
        return await delete_path(path, recursive=recursive, sandbox=sandbox)
    if operation == "mkdir":
        if not path:
            return ToolResult.failure("invalid_arguments", "mkdir requires path")
        return await make_directory(path, parents=parents, sandbox=sandbox)
    return ToolResult.failure("invalid_operation", f"Unknown path operation: {operation}")


async def move_path(
    source: str,
    destination: str,
    overwrite: bool = False,
    *,
    sandbox=None,
) -> ToolResult:
    """Move or rename one file, directory, or symbolic link.

    Args:
        source: Existing source path.
        destination: New path; its parent is created automatically.
        overwrite: Remove an existing destination before moving.
    """
    return await _structured_operation(
        "move",
        {"source": source, "destination": destination, "overwrite": overwrite},
        sandbox,
    )


async def copy_path(
    source: str,
    destination: str,
    overwrite: bool = False,
    *,
    sandbox=None,
) -> ToolResult:
    """Copy one file, directory, or symbolic link without decoding content.

    Args:
        source: Existing source path.
        destination: New path; its parent is created automatically.
        overwrite: Remove an existing destination before copying.
    """
    return await _structured_operation(
        "copy",
        {"source": source, "destination": destination, "overwrite": overwrite},
        sandbox,
    )


async def delete_path(
    path: str,
    recursive: bool = False,
    *,
    sandbox=None,
) -> ToolResult:
    """Delete one file, symbolic link, or directory.

    Non-empty directories require ``recursive=true``. This operation is
    destructive and remains subject to explicit tool and sandbox permission.

    Args:
        path: Existing path to delete.
        recursive: Recursively delete a non-empty directory.
    """
    return await _structured_operation(
        "delete", {"path": path, "recursive": recursive}, sandbox
    )


async def make_directory(
    path: str,
    parents: bool = True,
    *,
    sandbox=None,
) -> ToolResult:
    """Create an empty directory.

    Args:
        path: Directory path.
        parents: Create missing parent directories.
    """
    return await _structured_operation(
        "mkdir", {"path": path, "parents": parents}, sandbox
    )


# ----------------------------------------------------------------------
# search: content / name
# ----------------------------------------------------------------------


async def search(
    pattern: str,
    path: str = ".",
    mode: Literal["content", "name"] = "content",
    glob: str | None = None,
    max_results: int = 200,
    case_sensitive: bool = True,
    literal: bool = False,
    include_hidden: bool = False,
    exclude: list[str] | None = None,
    max_line_chars: int = 1000,
    kind: Literal["file", "directory", "any"] = "file",
    *,
    sandbox=None,
) -> ToolResult:
    """Search UTF-8 content or find paths by glob.

    ``mode`` selects the operation:

    - ``content`` (default): regex (or literal) search over UTF-8 files with
      bounded structured matches (path, line, column, clipped text).
    - ``name``: find paths recursively by glob pattern.

    Traversal stops at ``max_results``, skips symlinks and common dependency
    directories by default, and ignores non-UTF-8 files (content mode).

    Args:
        pattern: Regular expression (content) or glob (name).
        path: File to search, or root directory to search recursively.
        mode: Operation to perform.
        glob: Optional glob matched against relative paths or basenames (content).
        max_results: Maximum matches; must be at least one.
        case_sensitive: Use case-sensitive matching (content).
        literal: Escape ``pattern`` instead of interpreting it as regex (content).
        include_hidden: Search dotfiles and dot-directories.
        exclude: Directory or file names to skip during traversal.
        max_line_chars: Maximum text retained for each match (content).
        kind: Return files, directories, or both (name).
    """
    if mode == "content":
        return await search_text(
            pattern,
            path=path,
            glob=glob,
            max_results=max_results,
            case_sensitive=case_sensitive,
            literal=literal,
            include_hidden=include_hidden,
            exclude=exclude,
            max_line_chars=max_line_chars,
            sandbox=sandbox,
        )
    if mode == "name":
        return await find_files(
            pattern,
            path=path,
            max_results=max_results,
            kind=kind,
            include_hidden=include_hidden,
            exclude=exclude,
            sandbox=sandbox,
        )
    return ToolResult.failure("invalid_mode", f"Unknown search mode: {mode}")


async def search_text(
    pattern: str,
    path: str = ".",
    glob: str | None = None,
    max_results: int = 200,
    case_sensitive: bool = True,
    literal: bool = False,
    include_hidden: bool = False,
    exclude: list[str] | None = None,
    max_line_chars: int = 1000,
    *,
    sandbox=None,
) -> ToolResult:
    """Search UTF-8 files with bounded structured matches.

    Traversal stops at ``max_results``, skips symlinks and common dependency
    directories by default, and ignores non-UTF-8 files. Matches contain path,
    one-based line and column, clipped text, and a clipping flag.

    Args:
        pattern: Regular expression, or literal text when ``literal`` is true.
        path: UTF-8 file to search, or root directory to search recursively.
        glob: Optional glob matched against relative paths or basenames.
        max_results: Maximum matches; must be at least one.
        case_sensitive: Use case-sensitive matching.
        literal: Escape ``pattern`` instead of interpreting it as regex.
        include_hidden: Search dotfiles and dot-directories.
        exclude: Directory or file names to skip during directory traversal.
        max_line_chars: Maximum text retained for each match.
    """
    data = await _operation(
        "search",
        {
            "pattern": pattern,
            "path": path,
            "glob": glob,
            "max_results": max_results,
            "case_sensitive": case_sensitive,
            "literal": literal,
            "include_hidden": include_hidden,
            "exclude": exclude,
            "max_line_chars": max_line_chars,
        },
        sandbox,
    )
    return _data_result(data)


async def find_files(
    pattern: str = "*",
    path: str = ".",
    max_results: int = 500,
    kind: Literal["file", "directory", "any"] = "file",
    include_hidden: bool = False,
    exclude: list[str] | None = None,
    *,
    sandbox=None,
) -> ToolResult:
    """Find paths recursively with bounded glob matching.

    Matching is consistent in host and sandbox modes, symlinks are not
    followed, and traversal stops at ``max_results``.

    Args:
        pattern: Glob matched against relative paths and, without a slash, basenames.
        path: Root directory.
        max_results: Maximum paths; must be at least one.
        kind: Return files, directories, or both.
        include_hidden: Include dotfiles and dot-directories.
        exclude: Names to skip; defaults to common generated directories.
    """
    data = await _operation(
        "find",
        {
            "pattern": pattern,
            "path": path,
            "max_results": max_results,
            "kind": kind,
            "include_hidden": include_hidden,
            "exclude": exclude,
        },
        sandbox,
    )
    return _data_result(data)


# ----------------------------------------------------------------------
# Shared dispatch helpers
# ----------------------------------------------------------------------


def filesystem_tools() -> tuple[Tool, ...]:
    """The four merged model-facing filesystem tools."""
    return (
        Tool.from_function(read, name="read"),
        Tool.from_function(edit, name="edit"),
        Tool.from_function(path, name="path"),
        Tool.from_function(search, name="search"),
    )


FILESYSTEM_TOOLS = filesystem_tools()


async def _structured_operation(
    operation: str,
    args: dict[str, Any],
    sandbox: Any,
) -> ToolResult:
    return _data_result(await _operation(operation, args, sandbox))


async def _operation(
    operation: str,
    args: dict[str, Any],
    sandbox: Any,
) -> dict[str, Any]:
    resolved = _resolved_args(operation, args, sandbox)
    path = resolved.get("path")
    versions = (
        _FILE_VERSIONS.setdefault(sandbox, {})
        if sandbox is not None
        else None
    )

    effective_args = dict(resolved)
    if (
        versions is not None
        and isinstance(path, str)
        and operation in {"write", "edit", "patch"}
        and path in versions
    ):
        effective_args["expected_sha256"] = versions[path]

    if sandbox is not None and sandbox.enabled:
        result = _parse_result(await sandbox.filesystem(operation, effective_args))
    else:
        result = execute(
            operation,
            _resolved_args(operation, effective_args, sandbox),
        )

    if (
        not result.get("ok")
        or versions is None
        or not isinstance(path, str)
        or operation not in {"read", "stat", "write", "edit", "patch"}
    ):
        return result

    current = result.get("sha256")
    previous = versions.get(path)
    if (
        operation in {"read", "stat"}
        and path in versions
        and previous != current
    ):
        result["changed_since_last_observation"] = True
        result["previous_sha256"] = previous
    if isinstance(current, str):
        versions[path] = current
    else:
        versions.pop(path, None)
    return result


def _resolved_args(
    operation: str,
    args: dict[str, Any],
    sandbox: Any,
) -> dict[str, Any]:
    if sandbox is not None:
        return sandbox.resolve_filesystem_args(operation, args)
    resolved = dict(args)
    for field, _access in PATH_ACCESS.get(operation, ()):
        resolved[field] = str(
            Path(str(args[field])).expanduser().absolute()
        )
    return resolved


def _parse_result(value: str) -> dict[str, Any]:
    try:
        result = json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return {
            "ok": False,
            "error": {
                "code": "invalid_result",
                "message": "Filesystem backend returned invalid JSON",
            },
        }
    return result if isinstance(result, dict) else {
        "ok": False,
        "error": {"code": "invalid_result", "message": "Filesystem backend returned a non-object"},
    }


def _data_result(data: dict[str, Any], content: str | None = None) -> ToolResult:
    if not data.get("ok"):
        return _failure(data)
    if content is not None:
        return ToolResult.success(content)
    return ToolResult.success(json.dumps(data, ensure_ascii=False, sort_keys=True))


def _failure(data: dict[str, Any]) -> ToolResult:
    error = data.get("error") or {}
    failed = ToolResult.failure(
        str(error.get("code") or "filesystem_error"),
        str(error.get("message") or "Filesystem operation failed"),
    )
    return ToolResult(
        status=failed.status,
        content=failed.content,
        error=failed.error,
    )



def _with_line_numbers(content: str, first_line: int) -> str:
    lines = content.splitlines(keepends=True)
    if not lines:
        return content
    width = len(str(first_line + len(lines) - 1))
    return "".join(
        f"{number:>{width}}: {line}"
        for number, line in enumerate(lines, first_line)
    )


__all__ = [
    "FILESYSTEM_TOOLS",
    "copy_path",
    "delete_path",
    "edit",
    "edit_file",
    "filesystem_tools",
    "find_files",
    "list_files",
    "make_directory",
    "move_path",
    "path",
    "patch_file",
    "read",
    "read_bytes_file",
    "read_file",
    "search",
    "search_text",
    "stat_path",
    "write_file",
]
