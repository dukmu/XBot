"""Filesystem tools — read, write, and list files with metadata."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Literal

from langchain_core.tools import tool as langchain_tool

WriteMode = Literal[
    "overwrite",
    "append",
    "prepend",
    "insert_line",
    "replace_lines",
    "regex_replace",
    "apply_patch",
]


@langchain_tool
def filesystem_read(path: str, offset: int = 0, limit: int = 2000) -> str:
    """Read a text file and return JSON with content and file metadata.

    Args:
        path: Path to the file.
        offset: Zero-based line offset to start reading from.
        limit: Maximum number of lines to include.
    """
    p = Path(path)
    if not p.exists():
        return _json_error("file_not_found", f"File not found: {path}", path=path)
    if not p.is_file():
        return _json_error("not_a_file", f"Not a file: {path}", path=path)

    try:
        text = p.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return _json_error("not_text", f"File is not valid UTF-8 text: {path}", path=path)
    except Exception as exc:
        return _json_error("read_failed", f"Error reading {path}: {exc}", path=path)

    lines = text.splitlines()
    start = max(0, offset)
    end = len(lines) if limit <= 0 else min(len(lines), start + limit)
    selected = lines[start:end]
    stat = p.stat()

    return _json_ok({
        "path": str(p),
        "resolved_path": str(p.resolve()),
        "kind": "file",
        "size_bytes": stat.st_size,
        "mtime": stat.st_mtime,
        "line_count": len(lines),
        "offset": start,
        "limit": limit,
        "returned_lines": len(selected),
        "truncated_before": start > 0,
        "truncated_after": end < len(lines),
        "content": "\n".join(selected),
    })


@langchain_tool
def filesystem_write(
    path: str,
    content: str = "",
    mode: WriteMode = "overwrite",
    line: int | None = None,
    start_line: int | None = None,
    end_line: int | None = None,
    pattern: str | None = None,
    replacement: str = "",
) -> str:
    """Write or patch a text file and return JSON metadata.

    Modes:
        overwrite: Replace the whole file with content.
        append: Append content to the end of the file.
        prepend: Insert content at the beginning of the file.
        insert_line: Insert content before one-based line number ``line``.
        replace_lines: Replace inclusive one-based ``start_line``..``end_line``.
        regex_replace: Replace regex ``pattern`` with ``replacement``.
        apply_patch: Apply a unified diff from ``content`` to the current file.
    """
    p = Path(path)
    before = _read_existing_text(p)
    if before["error"]:
        return before["error"]

    old_text = before["text"]
    try:
        new_text, edit_meta = _apply_write_mode(
            old_text=old_text,
            content=content,
            mode=mode,
            line=line,
            start_line=start_line,
            end_line=end_line,
            pattern=pattern,
            replacement=replacement,
        )
    except ValueError as exc:
        return _json_error("invalid_write", str(exc), path=path, mode=mode)

    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(new_text, encoding="utf-8")
    except Exception as exc:
        return _json_error("write_failed", f"Error writing {path}: {exc}", path=path)

    stat = p.stat()
    return _json_ok({
        "path": str(p),
        "resolved_path": str(p.resolve()),
        "mode": mode,
        "bytes_written": len(new_text.encode("utf-8")),
        "size_bytes": stat.st_size,
        "mtime": stat.st_mtime,
        "line_count": len(new_text.splitlines()),
        "changed": old_text != new_text,
        **edit_meta,
    })


@langchain_tool
def filesystem_list(
    path: str = ".",
    recursive: bool = False,
    max_entries: int = 500,
) -> str:
    """List files/directories and return JSON metadata.

    Args:
        path: Directory path to list.
        recursive: If true, include descendants recursively.
        max_entries: Maximum entries returned before truncation.
    """
    p = Path(path)
    if not p.exists():
        return _json_error("path_not_found", f"Path not found: {path}", path=path)
    if not p.is_dir():
        return _json_error("not_a_directory", f"Not a directory: {path}", path=path)

    try:
        iterator = p.rglob("*") if recursive else p.iterdir()
        entries = sorted(iterator, key=lambda x: (not x.is_dir(), str(x)))
        limited = entries[:max_entries] if max_entries > 0 else entries
        return _json_ok({
            "path": str(p),
            "resolved_path": str(p.resolve()),
            "kind": "directory",
            "recursive": recursive,
            "entry_count": len(entries),
            "returned_entries": len(limited),
            "truncated": max_entries > 0 and len(entries) > max_entries,
            "entries": [_entry_metadata(entry, root=p) for entry in limited],
        })
    except Exception as exc:
        return _json_error("list_failed", f"Error listing {path}: {exc}", path=path)


def _apply_write_mode(
    *,
    old_text: str,
    content: str,
    mode: WriteMode,
    line: int | None,
    start_line: int | None,
    end_line: int | None,
    pattern: str | None,
    replacement: str,
) -> tuple[str, dict[str, Any]]:
    if mode == "overwrite":
        return content, {"operation": "overwrite"}
    if mode == "append":
        return old_text + content, {"operation": "append"}
    if mode == "prepend":
        return content + old_text, {"operation": "prepend"}
    if mode == "insert_line":
        if line is None or line < 1:
            raise ValueError("insert_line requires one-based line >= 1")
        lines = old_text.splitlines(keepends=True)
        index = min(line - 1, len(lines))
        lines.insert(index, _ensure_line_ending(content))
        return "".join(lines), {"operation": "insert_line", "line": line}
    if mode == "replace_lines":
        if start_line is None or end_line is None or start_line < 1 or end_line < start_line:
            raise ValueError("replace_lines requires 1 <= start_line <= end_line")
        lines = old_text.splitlines(keepends=True)
        start = min(start_line - 1, len(lines))
        end = min(end_line, len(lines))
        lines[start:end] = [_ensure_line_ending(content)]
        return "".join(lines), {
            "operation": "replace_lines",
            "start_line": start_line,
            "end_line": end_line,
        }
    if mode == "regex_replace":
        if not pattern:
            raise ValueError("regex_replace requires pattern")
        new_text, count = re.subn(pattern, replacement, old_text, flags=re.MULTILINE)
        return new_text, {"operation": "regex_replace", "replacements": count}
    if mode == "apply_patch":
        return _apply_unified_diff(old_text, content), {"operation": "apply_patch"}
    raise ValueError(f"Unknown write mode: {mode}")


def _apply_unified_diff(old_text: str, patch_text: str) -> str:
    old_lines = old_text.splitlines(keepends=True)
    patch_lines = patch_text.splitlines(keepends=True)
    if not patch_lines:
        return old_text

    out: list[str] = []
    old_index = 0
    i = 0
    saw_hunk = False

    while i < len(patch_lines):
        line = patch_lines[i]
        if line.startswith(("--- ", "+++ ")):
            i += 1
            continue
        if not line.startswith("@@ "):
            i += 1
            continue

        match = re.match(r"@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@", line)
        if not match:
            raise ValueError(f"Invalid unified diff hunk header: {line.rstrip()}")

        saw_hunk = True
        hunk_old_start = int(match.group(1)) - 1
        if hunk_old_start < old_index:
            raise ValueError("Overlapping unified diff hunks are not supported")

        out.extend(old_lines[old_index:hunk_old_start])
        old_index = hunk_old_start
        i += 1

        while i < len(patch_lines) and not patch_lines[i].startswith("@@ "):
            raw = patch_lines[i]
            if raw.startswith("\\ No newline at end of file"):
                i += 1
                continue
            if not raw:
                i += 1
                continue

            marker = raw[0]
            value = raw[1:]
            if marker == " ":
                _expect_old_line(old_lines, old_index, value)
                out.append(old_lines[old_index])
                old_index += 1
            elif marker == "-":
                _expect_old_line(old_lines, old_index, value)
                old_index += 1
            elif marker == "+":
                out.append(value)
            elif raw.startswith(("--- ", "+++ ")):
                pass
            else:
                raise ValueError(f"Invalid unified diff line: {raw.rstrip()}")
            i += 1

    if not saw_hunk:
        raise ValueError("apply_patch requires a unified diff with at least one hunk")

    out.extend(old_lines[old_index:])
    return "".join(out)


def _expect_old_line(old_lines: list[str], index: int, expected: str) -> None:
    if index >= len(old_lines):
        raise ValueError("Unified diff hunk extends past end of file")
    if old_lines[index] != expected:
        raise ValueError(
            "Unified diff context mismatch at line "
            f"{index + 1}: expected {expected.rstrip()!r}, "
            f"found {old_lines[index].rstrip()!r}"
        )


def _entry_metadata(entry: Path, *, root: Path) -> dict[str, Any]:
    stat = entry.stat()
    return {
        "name": entry.name,
        "path": str(entry),
        "relative_path": str(entry.relative_to(root)),
        "kind": "directory" if entry.is_dir() else "file",
        "size_bytes": stat.st_size,
        "mtime": stat.st_mtime,
    }


def _read_existing_text(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"text": "", "error": None}
    if not path.is_file():
        return {
            "text": "",
            "error": _json_error("not_a_file", f"Not a file: {path}", path=str(path)),
        }
    try:
        return {"text": path.read_text(encoding="utf-8"), "error": None}
    except UnicodeDecodeError:
        return {
            "text": "",
            "error": _json_error("not_text", f"File is not valid UTF-8 text: {path}", path=str(path)),
        }


def _ensure_line_ending(content: str) -> str:
    return content if content.endswith("\n") else content + "\n"


def _json_ok(payload: dict[str, Any]) -> str:
    return json.dumps({"ok": True, **payload}, ensure_ascii=False)


def _json_error(code: str, message: str, **extra: Any) -> str:
    return json.dumps({"ok": False, "error": {"code": code, "message": message}, **extra}, ensure_ascii=False)


FILESYSTEM_TOOLS = [filesystem_read, filesystem_write, filesystem_list]
