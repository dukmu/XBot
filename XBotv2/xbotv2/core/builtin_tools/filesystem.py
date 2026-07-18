"""Bounded, structured filesystem tools."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

from xbotv2.api.tools import Tool, ToolResult
from xbotv2.tools.filesystem_ops import PATH_ACCESS, execute


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
    """Read a bounded UTF-8 text range or inspect a non-text file.

    Text reads are limited by both line count and character count. When a
    single line exceeds ``max_chars``, continue with the returned
    ``next_offset`` and ``next_char_offset``. Non-UTF-8 files return metadata
    instead of binary content, including MIME type, size, SHA-256, and image
    dimensions when recognized. The ``session/`` virtual path is read-only.

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
    data["requested_path"] = path
    if not data.get("is_text", True):
        image = data.get("image") or {}
        dimensions = (
            f", {image.get('width')}x{image.get('height')} {image.get('format')}"
            if image else ""
        )
        content = (
            f"Non-text file: {path} ({data.get('media_type')}, "
            f"{data.get('size_bytes')} bytes{dimensions}, sha256={data.get('sha256')})"
        )
    elif line_numbers:
        content = _with_line_numbers(content, offset + 1)
    return ToolResult.success(content, data=data)


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
    return _data_result(data, _entry_lines(data, "entries"))


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
        path: Root directory to search recursively.
        glob: Optional glob matched against relative paths or basenames.
        max_results: Maximum matches; must be at least one.
        case_sensitive: Use case-sensitive matching.
        literal: Escape ``pattern`` instead of interpreting it as regex.
        include_hidden: Search dotfiles and dot-directories.
        exclude: Directory or file names to skip; defaults to common generated directories.
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
    lines = [
        f"{item['path']}:{item['line']}:{item['column']}:{item['text']}"
        for item in data.get("matches", [])
    ]
    return _data_result(data, "\n".join(lines))


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
    return _data_result(data, "\n".join(data.get("files", [])))


async def write_file(
    path: str,
    content: str,
    expected_sha256: str | None = None,
    *,
    sandbox=None,
) -> ToolResult:
    """Atomically create or completely replace one UTF-8 text file.

    Use ``filesystem_edit`` for a localized change and ``filesystem_patch`` for
    a unified diff. Existing non-UTF-8 files are rejected. Supplying the hash
    returned by ``filesystem_read`` prevents overwriting a concurrently changed
    file. Parent directories are created automatically.

    Args:
        path: Destination path relative to the workspace unless explicitly approved.
        content: Complete UTF-8 file content.
        expected_sha256: Optional hash that the current file must match.
    """
    return await _structured_operation(
        "write",
        {"path": path, "content": content, "expected_sha256": expected_sha256},
        sandbox,
    )


async def edit_file(
    path: str,
    old_text: str,
    new_text: str,
    replace_all: bool = False,
    expected_sha256: str | None = None,
    *,
    sandbox=None,
) -> ToolResult:
    """Atomically replace exact text in an existing UTF-8 file.

    The edit fails when ``old_text`` is absent or ambiguous. Set
    ``replace_all`` only when every occurrence should change. Use surrounding
    text to make a single replacement unambiguous.

    Args:
        path: Existing text file.
        old_text: Exact non-empty text expected in the file.
        new_text: Replacement text.
        replace_all: Replace every occurrence instead of requiring one match.
        expected_sha256: Optional hash that the current file must match.
    """
    return await _structured_operation(
        "edit",
        {
            "path": path,
            "old_text": old_text,
            "new_text": new_text,
            "replace_all": replace_all,
            "expected_sha256": expected_sha256,
        },
        sandbox,
    )


async def patch_file(
    path: str,
    patch: str,
    expected_sha256: str | None = None,
    *,
    sandbox=None,
) -> ToolResult:
    """Apply a validated unified diff to one UTF-8 file.

    The system ``patch`` implementation performs a dry run before applying any
    hunk. The patch must target only ``path``. Use separate calls for multiple
    files so permission and change records retain exact paths.

    Args:
        path: File created, updated, or deleted by the patch.
        patch: Complete unified diff with file headers and at least one hunk.
        expected_sha256: Optional hash that an existing target must match.
    """
    return await _structured_operation(
        "patch",
        {"path": path, "patch": patch, "expected_sha256": expected_sha256},
        sandbox,
    )


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
    if sandbox is not None and sandbox.enabled:
        return _parse_result(await sandbox.filesystem(operation, args))
    return execute(operation, _resolved_args(operation, args, sandbox))


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
    visible = content if content is not None else _summary(data)
    return ToolResult.success(visible, data=data)


def _failure(data: dict[str, Any]) -> ToolResult:
    error = data.get("error") or {}
    failed = ToolResult.failure(
        str(error.get("code") or "filesystem_error"),
        str(error.get("message") or "Filesystem operation failed"),
    )
    return ToolResult(
        status=failed.status,
        content=failed.content,
        data=data,
        error=failed.error,
    )


def _summary(data: dict[str, Any]) -> str:
    for action in ("deleted", "moved", "copied", "created", "changed"):
        if data.get(action):
            path = data.get("path") or data.get("destination") or ""
            return f"{action.capitalize()}: {path}"
    return json.dumps(data, ensure_ascii=False, sort_keys=True)


def _entry_lines(data: dict[str, Any], key: str) -> str:
    return "\n".join(
        f"{entry.get('kind', 'file')}\t{entry.get('relative_path', entry.get('path', ''))}"
        for entry in data.get(key, [])
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


filesystem_read = Tool.from_function(read_file, name="filesystem_read")
filesystem_stat = Tool.from_function(stat_path, name="filesystem_stat")
filesystem_list = Tool.from_function(list_files, name="filesystem_list")
filesystem_search = Tool.from_function(search_text, name="search_text")
filesystem_find = Tool.from_function(find_files, name="find_files")
filesystem_write = Tool.from_function(write_file, name="filesystem_write")
filesystem_edit = Tool.from_function(edit_file, name="filesystem_edit")
filesystem_patch = Tool.from_function(patch_file, name="filesystem_patch")
filesystem_move = Tool.from_function(move_path, name="filesystem_move")
filesystem_copy = Tool.from_function(copy_path, name="filesystem_copy")
filesystem_delete = Tool.from_function(delete_path, name="filesystem_delete")
filesystem_mkdir = Tool.from_function(make_directory, name="filesystem_mkdir")

FILESYSTEM_TOOLS = [
    filesystem_read,
    filesystem_stat,
    filesystem_list,
    filesystem_search,
    filesystem_find,
    filesystem_write,
    filesystem_edit,
    filesystem_patch,
    filesystem_move,
    filesystem_copy,
    filesystem_delete,
    filesystem_mkdir,
]


__all__ = [
    "FILESYSTEM_TOOLS",
    "copy_path",
    "delete_path",
    "edit_file",
    "filesystem_copy",
    "filesystem_delete",
    "filesystem_edit",
    "filesystem_find",
    "filesystem_list",
    "filesystem_mkdir",
    "filesystem_move",
    "filesystem_patch",
    "filesystem_read",
    "filesystem_search",
    "filesystem_stat",
    "filesystem_write",
    "find_files",
    "list_files",
    "make_directory",
    "move_path",
    "patch_file",
    "read_file",
    "search_text",
    "stat_path",
    "write_file",
]
