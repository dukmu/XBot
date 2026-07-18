"""Canonical UTF-8 filesystem operations used on the host and in bwrap."""

from __future__ import annotations

import fnmatch
import hashlib
import json
import mimetypes
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Literal


DEFAULT_EXCLUDES = (".git", ".venv", "node_modules", "__pycache__")
PathAccess = Literal["read", "write"]
PATH_ACCESS: dict[str, tuple[tuple[str, PathAccess], ...]] = {
    "read": (("path", "read"),),
    "stat": (("path", "read"),),
    "list": (("path", "read"),),
    "search": (("path", "read"),),
    "find": (("path", "read"),),
    "write": (("path", "write"),),
    "edit": (("path", "write"),),
    "patch": (("path", "write"),),
    "move": (("source", "write"), ("destination", "write")),
    "copy": (("source", "read"), ("destination", "write")),
    "delete": (("path", "write"),),
    "mkdir": (("path", "write"),),
}
TOOL_OPERATIONS = {
    "filesystem_read": "read",
    "filesystem_stat": "stat",
    "filesystem_list": "list",
    "search_text": "search",
    "find_files": "find",
    "filesystem_write": "write",
    "filesystem_edit": "edit",
    "filesystem_patch": "patch",
    "filesystem_move": "move",
    "filesystem_copy": "copy",
    "filesystem_delete": "delete",
    "filesystem_mkdir": "mkdir",
}


class FilesystemError(Exception):
    def __init__(self, code: str, message: str, **data: Any) -> None:
        super().__init__(message)
        self.code = code
        self.data = data


def execute(operation: str, args: dict[str, Any]) -> dict[str, Any]:
    handlers = {
        "read": _read,
        "stat": _stat,
        "list": _list,
        "search": _search,
        "find": _find,
        "write": _write,
        "edit": _edit,
        "patch": _patch,
        "move": _move,
        "copy": _copy,
        "delete": _delete,
        "mkdir": _mkdir,
    }
    handler = handlers.get(operation)
    if handler is None:
        return _error("invalid_operation", f"Unknown filesystem operation: {operation}")
    try:
        return {"ok": True, **handler(**args)}
    except FilesystemError as exc:
        return _error(exc.code, str(exc), **exc.data)
    except OSError as exc:
        return _error("filesystem_error", str(exc))
    except (TypeError, ValueError, re.error) as exc:
        return _error("invalid_arguments", str(exc))


def _read(
    path: str,
    offset: int = 0,
    limit: int = 2000,
    char_offset: int = 0,
    max_chars: int = 12000,
) -> dict[str, Any]:
    target = _file(path)
    if offset < 0 or char_offset < 0 or limit < 1 or max_chars < 1:
        raise FilesystemError(
            "invalid_range",
            "offset and char_offset must be >= 0; limit and max_chars must be >= 1",
            path=path,
        )
    metadata = _file_metadata(target, inspect_text=False)
    handle = target.open("r", encoding="utf-8", newline="")

    parts: list[str] = []
    chars = 0
    total_lines = 0
    returned_lines = 0
    next_offset = offset
    next_char_offset = char_offset
    partial = False
    first_selected_seen = False
    try:
        with handle:
            sample = handle.read(8192)
            if _contains_binary_controls(sample):
                return {**metadata, "is_text": False, "content": ""}
            handle.seek(0)
            for index, line in enumerate(handle):
                total_lines = index + 1
                if index < offset or partial or returned_lines >= limit:
                    continue
                skip = char_offset if not first_selected_seen else 0
                first_selected_seen = True
                if skip > len(line):
                    raise FilesystemError(
                        "invalid_range",
                        f"char_offset {char_offset} exceeds line {offset + 1}",
                        path=path,
                    )
                segment = line[skip:]
                remaining = max_chars - chars
                if len(segment) > remaining:
                    parts.append(segment[:remaining])
                    chars += remaining
                    next_offset = index
                    next_char_offset = skip + remaining
                    partial = True
                    continue
                parts.append(segment)
                chars += len(segment)
                returned_lines += 1
                next_offset = index + 1
                next_char_offset = 0
    except UnicodeDecodeError:
        return {**metadata, "is_text": False, "content": ""}

    content = "".join(parts)
    truncated_after = partial or next_offset < total_lines
    return {
        **metadata,
        "is_text": True,
        "line_count": total_lines,
        "offset": offset,
        "char_offset": char_offset,
        "limit": limit,
        "max_chars": max_chars,
        "returned_lines": returned_lines,
        "returned_chars": len(content),
        "truncated_before": offset > 0 or char_offset > 0,
        "truncated_after": truncated_after,
        "next_offset": next_offset if truncated_after else None,
        "next_char_offset": next_char_offset if truncated_after else None,
        "content": content,
    }


def _stat(path: str) -> dict[str, Any]:
    target = Path(path)
    if not target.exists() and not target.is_symlink():
        raise FilesystemError("path_not_found", f"Path not found: {path}", path=path)
    if target.is_symlink():
        stat = target.lstat()
        return {
            "path": str(target),
            "resolved_path": str(target.resolve(strict=False)),
            "kind": "symlink",
            "target": os.readlink(target),
            "size_bytes": stat.st_size,
            "mtime": stat.st_mtime,
        }
    if target.is_dir():
        stat = target.stat()
        return {
            "path": str(target),
            "resolved_path": str(target.resolve()),
            "kind": "directory",
            "size_bytes": stat.st_size,
            "mtime": stat.st_mtime,
        }
    if not target.is_file():
        raise FilesystemError("unsupported_file_type", f"Unsupported file type: {path}", path=path)
    return _file_metadata(target, inspect_text=True)


def _list(
    path: str,
    recursive: bool = False,
    max_entries: int = 500,
    include_hidden: bool = True,
) -> dict[str, Any]:
    root = _directory(path)
    if max_entries < 1:
        raise FilesystemError("invalid_limit", "max_entries must be >= 1", path=path)
    entries: list[dict[str, Any]] = []
    truncated = False
    for candidate in _walk_entries(root, recursive=recursive, include_hidden=include_hidden):
        if len(entries) >= max_entries:
            truncated = True
            break
        entries.append(_entry_metadata(candidate, root))
    return {
        "path": str(root),
        "resolved_path": str(root.resolve()),
        "kind": "directory",
        "recursive": recursive,
        "returned_entries": len(entries),
        "truncated": truncated,
        "entries": entries,
    }


def _search(
    pattern: str,
    path: str,
    glob: str | None = None,
    max_results: int = 200,
    case_sensitive: bool = True,
    literal: bool = False,
    include_hidden: bool = False,
    exclude: list[str] | None = None,
    max_line_chars: int = 1000,
) -> dict[str, Any]:
    root = _directory(path)
    if not pattern:
        raise FilesystemError("invalid_pattern", "pattern must be non-empty", path=path)
    if max_results < 1 or max_line_chars < 1:
        raise FilesystemError("invalid_limit", "result limits must be >= 1", path=path)
    flags = 0 if case_sensitive else re.IGNORECASE
    expression = re.compile(re.escape(pattern) if literal else pattern, flags)
    matches: list[dict[str, Any]] = []
    truncated = False
    for candidate in _walk_files(root, include_hidden, exclude):
        relative = candidate.relative_to(root).as_posix()
        if glob and not _glob_matches(relative, glob):
            continue
        try:
            handle = candidate.open("r", encoding="utf-8", newline="")
            with handle:
                for number, line in enumerate(handle, 1):
                    match = expression.search(line)
                    if match is None:
                        continue
                    if len(matches) >= max_results:
                        truncated = True
                        break
                    text = line.rstrip("\r\n")
                    matches.append({
                        "path": relative,
                        "line": number,
                        "column": match.start() + 1,
                        "text": text[:max_line_chars],
                        "text_truncated": len(text) > max_line_chars,
                    })
        except (OSError, UnicodeDecodeError):
            continue
        if truncated:
            break
    return {
        "path": str(root),
        "pattern": pattern,
        "matches": matches,
        "returned_matches": len(matches),
        "truncated": truncated,
    }


def _find(
    pattern: str,
    path: str,
    max_results: int = 500,
    kind: str = "file",
    include_hidden: bool = False,
    exclude: list[str] | None = None,
) -> dict[str, Any]:
    root = _directory(path)
    if max_results < 1 or kind not in {"file", "directory", "any"}:
        raise FilesystemError(
            "invalid_arguments",
            "max_results must be >= 1 and kind must be file, directory, or any",
        )
    matches: list[str] = []
    truncated = False
    for candidate in _walk_entries(
        root,
        recursive=True,
        include_hidden=include_hidden,
        exclude=exclude,
    ):
        candidate_kind = "directory" if candidate.is_dir() else "file"
        if kind != "any" and candidate_kind != kind:
            continue
        relative = candidate.relative_to(root).as_posix()
        if not _glob_matches(relative, pattern):
            continue
        if len(matches) >= max_results:
            truncated = True
            break
        matches.append(relative)
    return {
        "path": str(root),
        "pattern": pattern,
        "kind": kind,
        "files": matches,
        "returned_files": len(matches),
        "truncated": truncated,
    }


def _write(path: str, content: str, expected_sha256: str | None = None) -> dict[str, Any]:
    target = Path(path)
    before = _existing_text(target)
    _check_hash(target, before, expected_sha256)
    changed = before != content
    if changed:
        _atomic_write(target, content)
    return _write_metadata(target, before, content, created=before is None, changed=changed)


def _edit(
    path: str,
    old_text: str,
    new_text: str,
    replace_all: bool = False,
    expected_sha256: str | None = None,
) -> dict[str, Any]:
    if not old_text:
        raise FilesystemError("invalid_edit", "old_text must be non-empty", path=path)
    target = _file(path)
    before = _existing_text(target)
    assert before is not None
    _check_hash(target, before, expected_sha256)
    count = before.count(old_text)
    if count == 0:
        raise FilesystemError("text_not_found", "old_text was not found", path=path)
    if count > 1 and not replace_all:
        raise FilesystemError(
            "ambiguous_edit",
            f"old_text occurs {count} times; set replace_all=true or provide more context",
            path=path,
            occurrences=count,
        )
    after = before.replace(old_text, new_text, -1 if replace_all else 1)
    _atomic_write(target, after)
    return {
        **_write_metadata(target, before, after, created=False, changed=True),
        "replacements": count if replace_all else 1,
    }


def _patch(
    path: str,
    patch: str,
    expected_sha256: str | None = None,
) -> dict[str, Any]:
    target = Path(path)
    before = _existing_text(target)
    _check_hash(target, before, expected_sha256)
    if not patch.strip() or "@@ " not in patch:
        raise FilesystemError("invalid_patch", "patch must contain a unified diff hunk", path=path)
    _validate_patch_headers(target, patch)
    executable = shutil.which("patch")
    if executable is None:
        raise FilesystemError("patch_unavailable", "The system patch executable is not installed")

    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=f".{target.name}.patch-",
        dir=target.parent,
    ) as directory:
        candidate = Path(directory) / target.name
        if before is not None:
            candidate.write_text(before, encoding="utf-8", newline="")
            shutil.copymode(target, candidate)
        command = [executable, "--batch", "--forward", "--silent", "--reject-file=-"]
        _run_patch([*command, "--dry-run", str(candidate)], patch, target)
        _run_patch([*command, str(candidate)], patch, target)
        if candidate.exists():
            after = _existing_text(candidate)
            assert after is not None
            os.replace(candidate, target)
            return {
                **_write_metadata(
                    target,
                    before,
                    after,
                    created=before is None,
                    changed=before != after,
                ),
                "patched": True,
            }
        if target.exists():
            target.unlink()
        return {
            "path": str(target),
            "deleted": True,
            "before_sha256": _sha256_bytes(before.encode("utf-8")) if before is not None else None,
            "sha256": None,
            "patched": True,
        }


def _move(source: str, destination: str, overwrite: bool = False) -> dict[str, Any]:
    src = _path(source)
    dst = Path(destination)
    _prepare_destination(dst, overwrite)
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dst))
    return {"source": str(src), "destination": str(dst), "moved": True}


def _copy(source: str, destination: str, overwrite: bool = False) -> dict[str, Any]:
    src = _path(source)
    dst = Path(destination)
    _prepare_destination(dst, overwrite)
    dst.parent.mkdir(parents=True, exist_ok=True)
    if src.is_dir() and not src.is_symlink():
        shutil.copytree(src, dst)
    else:
        shutil.copy2(src, dst, follow_symlinks=False)
    return {"source": str(src), "destination": str(dst), "copied": True}


def _delete(path: str, recursive: bool = False) -> dict[str, Any]:
    target = _path(path)
    kind = "directory" if target.is_dir() and not target.is_symlink() else "file"
    if kind == "directory":
        if not recursive:
            target.rmdir()
        else:
            shutil.rmtree(target)
    else:
        target.unlink()
    return {"path": str(target), "kind": kind, "deleted": True}


def _mkdir(path: str, parents: bool = True) -> dict[str, Any]:
    target = Path(path)
    existed = target.is_dir()
    target.mkdir(parents=parents, exist_ok=True)
    return {"path": str(target), "created": not existed}


def _path(value: str) -> Path:
    path = Path(value)
    if not path.exists() and not path.is_symlink():
        raise FilesystemError("path_not_found", f"Path not found: {value}", path=value)
    return path


def _file(value: str) -> Path:
    path = Path(value)
    if not path.exists() and not path.is_symlink():
        raise FilesystemError("file_not_found", f"File not found: {value}", path=value)
    if not path.is_file():
        raise FilesystemError("not_a_file", f"Not a file: {value}", path=value)
    return path


def _directory(value: str) -> Path:
    path = _path(value)
    if not path.is_dir():
        raise FilesystemError("not_a_directory", f"Not a directory: {value}", path=value)
    return path


def _existing_text(path: Path) -> str | None:
    if not path.exists():
        return None
    if not path.is_file():
        raise FilesystemError("not_a_file", f"Not a file: {path}", path=str(path))
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        raise FilesystemError(
            "not_text",
            f"File is not valid UTF-8 text: {path}",
            path=str(path),
        ) from None


def _check_hash(path: Path, content: str | None, expected: str | None) -> None:
    if expected is None:
        return
    actual = _sha256_bytes(content.encode("utf-8")) if content is not None else None
    if actual != expected:
        raise FilesystemError(
            "content_changed",
            f"File changed since it was read: {path}",
            path=str(path),
            expected_sha256=expected,
            actual_sha256=actual,
        )


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = path.stat().st_mode if path.exists() else None
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        if mode is not None:
            os.chmod(temp_name, mode)
        os.replace(temp_name, path)
    except BaseException:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise


def _write_metadata(
    path: Path,
    before: str | None,
    after: str,
    *,
    created: bool,
    changed: bool,
) -> dict[str, Any]:
    stat = path.stat()
    return {
        "path": str(path),
        "resolved_path": str(path.resolve()),
        "created": created,
        "changed": changed,
        "size_bytes": stat.st_size,
        "mtime": stat.st_mtime,
        "line_count": len(after.splitlines()),
        "before_sha256": _sha256_bytes(before.encode("utf-8")) if before is not None else None,
        "sha256": _sha256_bytes(after.encode("utf-8")),
    }


def _file_metadata(path: Path, *, inspect_text: bool) -> dict[str, Any]:
    stat = path.stat()
    media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    result: dict[str, Any] = {
        "path": str(path),
        "resolved_path": str(path.resolve()),
        "kind": "file",
        "extension": path.suffix.lower(),
        "media_type": media_type,
        "size_bytes": stat.st_size,
        "mtime": stat.st_mtime,
        "sha256": _sha256_file(path),
    }
    if inspect_text:
        result["is_text"] = _is_text(path)
    image = _image_metadata(path)
    if image:
        result["image"] = image
        result["media_type"] = {
            "PNG": "image/png",
            "GIF": "image/gif",
            "JPEG": "image/jpeg",
        }[image["format"]]
    return result


def _entry_metadata(path: Path, root: Path) -> dict[str, Any]:
    stat = path.lstat()
    kind = "symlink" if path.is_symlink() else "directory" if path.is_dir() else "file"
    result = {
        "name": path.name,
        "path": str(path),
        "relative_path": path.relative_to(root).as_posix(),
        "kind": kind,
        "size_bytes": stat.st_size,
        "mtime": stat.st_mtime,
    }
    if kind == "symlink":
        result["target"] = os.readlink(path)
    return result


def _walk_entries(
    root: Path,
    *,
    recursive: bool,
    include_hidden: bool,
    exclude: list[str] | None = None,
):
    excluded = set(DEFAULT_EXCLUDES if exclude is None else exclude)
    if not recursive:
        for child in sorted(root.iterdir(), key=lambda item: (not item.is_dir(), item.name)):
            if _visible(child.name, include_hidden) and child.name not in excluded:
                yield child
        return
    for current, directories, files in os.walk(root, followlinks=False):
        directories[:] = sorted(
            name for name in directories
            if _visible(name, include_hidden) and name not in excluded
        )
        for name in directories + sorted(files):
            if _visible(name, include_hidden) and name not in excluded:
                yield Path(current) / name


def _walk_files(root: Path, include_hidden: bool, exclude: list[str] | None):
    for path in _walk_entries(
        root,
        recursive=True,
        include_hidden=include_hidden,
        exclude=exclude,
    ):
        if path.is_file() and not path.is_symlink():
            yield path


def _visible(name: str, include_hidden: bool) -> bool:
    return include_hidden or not name.startswith(".")


def _glob_matches(relative: str, pattern: str) -> bool:
    return fnmatch.fnmatch(relative, pattern) or (
        "/" not in pattern and fnmatch.fnmatch(Path(relative).name, pattern)
    )


def _prepare_destination(path: Path, overwrite: bool) -> None:
    if not path.exists() and not path.is_symlink():
        return
    if not overwrite:
        raise FilesystemError(
            "destination_exists",
            f"Destination already exists: {path}",
            path=str(path),
        )
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink()


def _validate_patch_headers(target: Path, patch: str) -> None:
    headers = [
        line[4:].split("\t", 1)[0].strip()
        for line in patch.splitlines()
        if line.startswith(("--- ", "+++ "))
    ]
    if len(headers) != 2:
        raise FilesystemError(
            "invalid_patch",
            "patch must contain exactly one old/new file header pair",
            path=str(target),
        )
    names = [
        Path(value.removeprefix("a/").removeprefix("b/")).name
        for value in headers
        if value != "/dev/null"
    ]
    if any(name != target.name for name in names):
        raise FilesystemError(
            "patch_path_mismatch",
            f"patch headers do not target {target.name}",
            path=str(target),
        )


def _run_patch(command: list[str], patch: str, target: Path) -> None:
    completed = subprocess.run(
        command,
        input=patch,
        text=True,
        encoding="utf-8",
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    if completed.returncode:
        detail = completed.stdout.strip() or "patch rejected"
        raise FilesystemError("patch_failed", detail, path=str(target))


def _is_text(path: Path) -> bool:
    try:
        with path.open("r", encoding="utf-8") as handle:
            for chunk in iter(lambda: handle.read(65536), ""):
                if _contains_binary_controls(chunk):
                    return False
        return True
    except UnicodeDecodeError:
        return False


def _contains_binary_controls(value: str) -> bool:
    if "\x00" in value:
        return True
    controls = sum(
        ord(character) < 32 and character not in "\b\t\n\f\r"
        for character in value
    )
    return bool(value) and controls / len(value) > 0.1


def _image_metadata(path: Path) -> dict[str, Any] | None:
    try:
        with path.open("rb") as handle:
            header = handle.read(32)
            if header.startswith(b"\x89PNG\r\n\x1a\n") and len(header) >= 24:
                width, height = struct.unpack(">II", header[16:24])
                return {"format": "PNG", "width": width, "height": height}
            if header[:6] in {b"GIF87a", b"GIF89a"} and len(header) >= 10:
                width, height = struct.unpack("<HH", header[6:10])
                return {"format": "GIF", "width": width, "height": height}
            if header.startswith(b"\xff\xd8"):
                handle.seek(2)
                while True:
                    prefix = handle.read(1)
                    if not prefix:
                        break
                    if prefix != b"\xff":
                        continue
                    marker = handle.read(1)
                    while marker == b"\xff":
                        marker = handle.read(1)
                    if not marker or marker in {b"\x00", b"\xd8"}:
                        continue
                    if marker == b"\x01" or 0xD0 <= marker[0] <= 0xD7:
                        continue
                    if marker in {b"\xd9", b"\xda"}:
                        break
                    length_data = handle.read(2)
                    if len(length_data) < 2:
                        break
                    length = struct.unpack(">H", length_data)[0]
                    if length < 2:
                        break
                    if marker[0] in {
                        0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                        0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF,
                    }:
                        data = handle.read(5)
                        if len(data) < 5:
                            break
                        height, width = struct.unpack(">HH", data[1:5])
                        return {"format": "JPEG", "width": width, "height": height}
                    handle.seek(length - 2, os.SEEK_CUR)
    except (OSError, struct.error):
        return None
    return None


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _error(code: str, message: str, **data: Any) -> dict[str, Any]:
    return {"ok": False, "error": {"code": code, "message": message}, **data}


def main() -> None:
    try:
        request = json.load(sys.stdin)
        result = execute(str(request.get("operation") or ""), dict(request.get("args") or {}))
    except Exception as exc:
        result = _error("invalid_request", str(exc))
    sys.stdout.write(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()


__all__ = ["DEFAULT_EXCLUDES", "PATH_ACCESS", "TOOL_OPERATIONS", "execute"]
