"""Filesystem tools — read, write, and list files. Use session sandbox capabilities when available."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Literal

from xbotv2.api.tools import ToolResult
from xbotv2.api.tools import Tool

WriteMode = Literal[
    "overwrite", "append", "prepend", "insert_line",
    "replace_lines", "regex_replace", "apply_patch",
]


async def read_file(path: str, offset: int = 0, limit: int = 2000, *, sandbox=None) -> ToolResult:
    """Read a bounded range of lines from one UTF-8 text file.

    Use this before editing an existing file and to inspect cached tool/context
    artifacts. Paths are workspace-relative unless they use the read-only
    ``session/`` mount. The result includes content, resolved path, file size,
    line count, and flags indicating omitted lines.

    Args:
        path: File path to read. It must identify a file, not a directory.
        offset: Zero-based first line to return. Use it to continue a bounded read.
        limit: Maximum lines to return. Values <= 0 read all remaining lines and
            should only be used after checking that the file is small.
    """
    if sandbox is not None and sandbox.enabled:
        return _tool_result_from_json(await sandbox.read_file(path, offset=offset, limit=limit))

    p = sandbox.resolve_read_path(path) if sandbox is not None else Path(path)
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
) -> ToolResult:
    """Create or edit one UTF-8 text file using an explicit write mode.

    Read an existing file before changing it. Prefer ``apply_patch`` or a narrow
    line/regex replacement for source edits; use ``append`` for incremental
    documents and ``overwrite`` only when replacing the complete file is
    intended. The result reports the resolved path, final size, line count,
    whether content changed, and mode-specific metadata.

    Args:
        path: Destination file path. Relative paths resolve inside the workspace.
        content: Text to write. For apply_patch this is a unified diff.
        mode: Operation: overwrite replaces the whole file; append/prepend add
            text; insert_line inserts before a one-based line; replace_lines
            replaces an inclusive one-based range; regex_replace applies pattern;
            apply_patch applies unified-diff hunks.
        line: One-based insertion line required by insert_line.
        start_line: First one-based line required by replace_lines.
        end_line: Last inclusive one-based line required by replace_lines.
        pattern: Python regular expression required by regex_replace.
        replacement: Replacement text used by regex_replace.
    """
    if sandbox is not None and sandbox.enabled:
        return await _sandboxed_write(sandbox, path, content, mode, line, start_line, end_line, pattern, replacement)

    p = Path(path)
    if sandbox is not None and not p.is_absolute():
        p = Path(sandbox.workspace_root) / p
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


async def _sandboxed_write(
    sandbox,
    path,
    content,
    mode,
    line,
    start_line,
    end_line,
    pattern,
    replacement,
):
    read_result = _parse_sandbox_result(
        await sandbox.read_file(path, offset=0, limit=0)
    )
    if read_result.get("ok"):
        old_text = str(read_result.get("content") or "")
    elif (read_result.get("error") or {}).get("code") == "file_not_found":
        old_text = ""
    else:
        return _tool_result_from_data(read_result)
    try:
        new_text, edit_meta = _apply_write_mode(
            old_text=old_text, content=content, mode=mode,
            line=line, start_line=start_line, end_line=end_line,
            pattern=pattern, replacement=replacement,
        )
    except ValueError as exc:
        return _json_error("invalid_write", str(exc), path=path, mode=mode)
    write_result = _parse_sandbox_result(await sandbox.write_file(path, new_text))
    if write_result.get("ok"):
        write_result.update(
            mode=mode,
            changed=old_text != new_text,
            **edit_meta,
        )
    return _tool_result_from_data(write_result)


async def list_files(path: str = ".", recursive: bool = False, max_entries: int = 500, *, sandbox=None) -> ToolResult:
    """List directory entries with bounded structured metadata.

    Use this to inspect directory shape before selecting files. It returns entry
    names, relative paths, kinds, sizes, and truncation metadata; it does not read
    file contents.

    Args:
        path: Directory path. Relative paths resolve inside the workspace;
            ``session/`` exposes the current session state read-only.
        recursive: When true, include descendants instead of direct children only.
        max_entries: Maximum entries returned. Values <= 0 are unbounded and
            should only be used for a directory already known to be small.
    """
    if sandbox is not None and sandbox.enabled:
        return _tool_result_from_json(
            await sandbox.list_dir(path, recursive=recursive, max_entries=max_entries)
        )
    p = sandbox.resolve_read_path(path) if sandbox is not None else Path(path)
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


async def search_text(
    pattern: str,
    path: str = ".",
    glob: str | None = None,
    max_results: int = 200,
    *,
    sandbox=None,
) -> ToolResult:
    """Search UTF-8 files recursively with a Python regular expression.

    Use this to locate text and line numbers before reading or patching files.
    Binary and invalid UTF-8 files are skipped. Results use
    ``relative_path:line_number:text`` and include total/truncation metadata.

    Args:
        pattern: Non-empty Python regular expression matched against each line.
        path: Root directory to search recursively.
        glob: Optional glob applied to each path relative to the search root.
        max_results: Maximum matching lines returned; <= 0 is unbounded.
    """
    if not pattern:
        return _json_error("invalid_pattern", "pattern must be non-empty", path=path)
    if sandbox is not None and sandbox.enabled:
        return _tool_result_from_json(
            await sandbox.search_text(pattern, path, glob, max_results)
        )
    root = sandbox.resolve_read_path(path) if sandbox is not None else Path(path)
    if not root.is_dir():
        return _json_error("not_a_directory", f"Not a directory: {path}", path=path)
    try:
        expression = re.compile(pattern)
    except re.error as exc:
        return _json_error("invalid_pattern", str(exc), path=path)
    import fnmatch

    matches: list[str] = []
    for candidate in root.rglob("*"):
        if not candidate.is_file():
            continue
        relative = str(candidate.relative_to(root))
        if glob and not fnmatch.fnmatch(relative, glob):
            continue
        try:
            lines = candidate.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            continue
        matches.extend(
            f"{relative}:{number}:{line}"
            for number, line in enumerate(lines, 1)
            if expression.search(line)
        )
    limited = matches[:max_results] if max_results > 0 else matches
    return _json_ok({
        "path": path,
        "pattern": pattern,
        "matches": limited,
        "match_count": len(matches),
        "returned_matches": len(limited),
        "truncated": max_results > 0 and len(matches) > max_results,
    })


async def find_files(
    pattern: str = "*",
    path: str = ".",
    max_results: int = 500,
    *,
    sandbox=None,
) -> ToolResult:
    """Find file paths recursively by glob without reading file content.

    Use this when filenames or extensions are known but locations are not. The
    result contains paths relative to the requested root plus count and
    truncation metadata.

    Args:
        pattern: Glob pattern such as ``*.py`` or ``**/test_*.py``.
        path: Root directory to search recursively.
        max_results: Maximum paths returned; <= 0 is unbounded.
    """
    if sandbox is not None and sandbox.enabled:
        listing = _parse_sandbox_result(
            await sandbox.list_dir(path, recursive=True, max_entries=0)
        )
        if not listing.get("ok"):
            return _tool_result_from_data(listing)
        paths = [
            str(entry.get("relative_path") or entry.get("path") or "")
            for entry in listing.get("entries", [])
            if entry.get("kind") == "file"
        ]
        import fnmatch

        matches = [candidate for candidate in paths if fnmatch.fnmatch(candidate, pattern)]
    else:
        root = sandbox.resolve_read_path(path) if sandbox is not None else Path(path)
        if not root.is_dir():
            return _json_error("not_a_directory", f"Not a directory: {path}", path=path)
        matches = sorted(
            str(candidate.relative_to(root))
            for candidate in root.rglob(pattern)
            if candidate.is_file()
        )
    limited = matches[:max_results] if max_results > 0 else matches
    return _json_ok({
        "path": path,
        "pattern": pattern,
        "files": limited,
        "file_count": len(matches),
        "returned_files": len(limited),
        "truncated": max_results > 0 and len(matches) > max_results,
    })

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


def _tool_result_from_json(value: str) -> ToolResult:
    return _tool_result_from_data(_parse_sandbox_result(value))


def _tool_result_from_data(data: dict[str, Any]) -> ToolResult:
    content = json.dumps(data, ensure_ascii=False)
    if data.get("ok", True):
        return ToolResult.success(content, data=data)
    error = data.get("error") or {}
    result = ToolResult.failure(
        str(error.get("code") or "tool_error"),
        str(error.get("message") or content),
    )
    return ToolResult(
        status=result.status,
        content=content,
        data=data,
        error=result.error,
    )


def _json_ok(payload: dict[str, Any]) -> ToolResult:
    return _tool_result_from_data({"ok": True, **payload})


def _json_error(code: str, message: str, **extra: Any) -> ToolResult:
    data = {"ok": False, "error": {"code": code, "message": message}, **extra}
    return _tool_result_from_data(data)


filesystem_read = Tool.from_function(read_file, name="filesystem_read")
filesystem_write = Tool.from_function(write_file, name="filesystem_write")
filesystem_list = Tool.from_function(list_files, name="filesystem_list")
filesystem_search = Tool.from_function(search_text, name="search_text")
filesystem_find = Tool.from_function(find_files, name="find_files")
FILESYSTEM_TOOLS = [
    filesystem_read,
    filesystem_write,
    filesystem_list,
    filesystem_search,
    filesystem_find,
]
