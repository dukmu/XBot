"""Filesystem tools — read, write, and list files. Use session sandbox capabilities when available."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Literal

from xbotv2.tools.types import XBotTool

WriteMode = Literal[
    "overwrite", "append", "prepend", "insert_line",
    "replace_lines", "regex_replace", "apply_patch",
]


async def read_file(path: str, offset: int = 0, limit: int = 2000, *, sandbox=None) -> str:
    """Read a text file and return JSON with content and file metadata."""
    if sandbox is not None and sandbox.enabled:
        return await sandbox.read_file(path, offset=offset, limit=limit)

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
        "path": str(p), "resolved_path": str(p.resolve()), "kind": "file",
        "size_bytes": stat.st_size, "mtime": stat.st_mtime, "line_count": len(lines),
        "offset": start, "limit": limit, "returned_lines": len(selected),
        "truncated_before": start > 0, "truncated_after": end < len(lines),
        "content": "\n".join(selected),
    })


async def write_file(
    path: str, content: str = "", mode: WriteMode = "overwrite",
    line: int | None = None, start_line: int | None = None, end_line: int | None = None,
    pattern: str | None = None, replacement: str = "",
    *, sandbox=None,
) -> str:
    """Write or patch a text file and return JSON metadata."""
    if sandbox is not None and sandbox.enabled:
        return await _sandboxed_write(sandbox, path, content, mode, line, start_line, end_line, pattern, replacement)

    p = Path(path)
    before = _read_existing_text(p)
    if before["error"]:
        return before["error"]
    old_text = before["text"]
    try:
        new_text, edit_meta = _apply_write_mode(
            old_text=old_text, content=content, mode=mode,
            line=line, start_line=start_line, end_line=end_line,
            pattern=pattern, replacement=replacement,
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
        "path": str(p), "resolved_path": str(p.resolve()), "mode": mode,
        "bytes_written": len(new_text.encode("utf-8")), "size_bytes": stat.st_size,
        "mtime": stat.st_mtime, "line_count": len(new_text.splitlines()),
        "changed": old_text != new_text, **edit_meta,
    })


async def _sandboxed_write(sandbox, path, content, mode, line, start_line, end_line, pattern, replacement):
    old_text = ""
    if mode != "overwrite":
        result_json = await sandbox.read_file(path, offset=0, limit=0)
        result = _parse_sandbox_result(result_json)
        if not result.get("ok"):
            return result_json
        old_text = result.get("content", "")
    try:
        new_text, edit_meta = _apply_write_mode(
            old_text=old_text, content=content, mode=mode,
            line=line, start_line=start_line, end_line=end_line,
            pattern=pattern, replacement=replacement,
        )
    except ValueError as exc:
        return _json_error("invalid_write", str(exc), path=path, mode=mode)
    return await sandbox.write_file(path, new_text)


async def list_files(path: str = ".", recursive: bool = False, max_entries: int = 500, *, sandbox=None) -> str:
    """List files/directories and return JSON metadata."""
    if sandbox is not None and sandbox.enabled:
        return await sandbox.list_dir(path, recursive=recursive, max_entries=max_entries)

    p = Path(path)
    if not p.exists():
        return _json_error("path_not_found", f"Path not found: {path}", path=path)
    if not p.is_dir():
        return _json_error("not_a_directory", f"Not a directory: {path}", path=path)
    try:
        it = p.rglob("*") if recursive else p.iterdir()
        entries = sorted(it, key=lambda x: (not x.is_dir(), str(x)))
        limited = entries[:max_entries] if max_entries > 0 else entries
        return _json_ok({
            "path": str(p), "resolved_path": str(p.resolve()), "kind": "directory",
            "recursive": recursive, "entry_count": len(entries),
            "returned_entries": len(limited),
            "truncated": max_entries > 0 and len(entries) > max_entries,
            "entries": [_entry_metadata(e, root=p) for e in limited],
        })
    except Exception as exc:
        return _json_error("list_failed", f"Error listing {path}: {exc}", path=path)


def _parse_sandbox_result(text: str) -> dict[str, Any]:
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return {"ok": True, "content": text}


def _apply_write_mode(*, old_text, content, mode, line, start_line, end_line, pattern, replacement):
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
        lines.insert(min(line - 1, len(lines)), _ensure_line_ending(content))
        return "".join(lines), {"operation": "insert_line", "line": line}
    if mode == "replace_lines":
        if start_line is None or end_line is None or start_line < 1 or end_line < start_line:
            raise ValueError("replace_lines requires 1 <= start_line <= end_line")
        lines = old_text.splitlines(keepends=True)
        start = min(start_line - 1, len(lines))
        end = min(end_line, len(lines))
        lines[start:end] = [_ensure_line_ending(content)]
        return "".join(lines), {"operation": "replace_lines", "start_line": start_line, "end_line": end_line}
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
    out, old_index, i, saw_hunk = [], 0, 0, False
    while i < len(patch_lines):
        line = patch_lines[i]
        if line.startswith(("--- ", "+++ ")):
            i += 1; continue
        if not line.startswith("@@ "):
            i += 1; continue
        m = re.match(r"@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@", line)
        if not m:
            raise ValueError(f"Invalid unified diff hunk header: {line.rstrip()}")
        saw_hunk = True
        hunk_old_start = int(m.group(1)) - 1
        if hunk_old_start < old_index:
            raise ValueError("Overlapping unified diff hunks are not supported")
        out.extend(old_lines[old_index:hunk_old_start])
        old_index = hunk_old_start
        i += 1
        while i < len(patch_lines) and not patch_lines[i].startswith("@@ "):
            raw = patch_lines[i]
            if raw.startswith("\\ No newline at end of file"):
                i += 1; continue
            if not raw:
                i += 1; continue
            marker, value = raw[0], raw[1:]
            if marker == " ":
                _expect_old_line(old_lines, old_index, value)
                out.append(old_lines[old_index]); old_index += 1
            elif marker == "-":
                _expect_old_line(old_lines, old_index, value); old_index += 1
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


def _expect_old_line(old_lines, index, expected):
    if index >= len(old_lines):
        raise ValueError("Unified diff hunk extends past end of file")
    if old_lines[index] != expected:
        raise ValueError(f"Unified diff context mismatch at line {index+1}: expected {expected.rstrip()!r}, found {old_lines[index].rstrip()!r}")


def _entry_metadata(entry: Path, *, root: Path) -> dict[str, Any]:
    stat = entry.stat()
    return {
        "name": entry.name, "path": str(entry),
        "relative_path": str(entry.relative_to(root)),
        "kind": "directory" if entry.is_dir() else "file",
        "size_bytes": stat.st_size, "mtime": stat.st_mtime,
    }


def _read_existing_text(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"text": "", "error": None}
    if not path.is_file():
        return {"text": "", "error": _json_error("not_a_file", f"Not a file: {path}", path=str(path))}
    try:
        return {"text": path.read_text(encoding="utf-8"), "error": None}
    except UnicodeDecodeError:
        return {"text": "", "error": _json_error("not_text", f"File is not valid UTF-8 text: {path}", path=str(path))}


def _ensure_line_ending(content: str) -> str:
    return content if content.endswith("\n") else content + "\n"


def _json_ok(payload: dict[str, Any]) -> str:
    return json.dumps({"ok": True, **payload}, ensure_ascii=False)


def _json_error(code: str, message: str, **extra: Any) -> str:
    return json.dumps({"ok": False, "error": {"code": code, "message": message}, **extra}, ensure_ascii=False)


filesystem_read = XBotTool.from_function(read_file, name="filesystem_read")
filesystem_write = XBotTool.from_function(write_file, name="filesystem_write")
filesystem_list = XBotTool.from_function(list_files, name="filesystem_list")
FILESYSTEM_TOOLS = [filesystem_read, filesystem_write, filesystem_list]
