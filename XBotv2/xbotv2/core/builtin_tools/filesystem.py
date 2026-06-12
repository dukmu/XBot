"""Filesystem tools — read, write, list files."""

from langchain_core.tools import tool as langchain_tool


@langchain_tool
def filesystem_read(path: str, offset: int = 0, limit: int = 2000) -> str:
    """Read a file from the filesystem.

    Args:
        path: Path to the file.
        offset: Line offset to start reading from.
        limit: Maximum number of lines to read.
    """
    from pathlib import Path
    p = Path(path)
    if not p.exists():
        return f"Error: file not found: {path}"
    try:
        lines = p.read_text().splitlines()
        if offset:
            lines = lines[offset:]
        if limit:
            lines = lines[:limit]
        return "\n".join(lines)
    except Exception as exc:
        return f"Error reading {path}: {exc}"


@langchain_tool
def filesystem_write(path: str, content: str) -> str:
    """Write content to a file.

    Args:
        path: Path to the file (created if missing).
        content: Text content to write.
    """
    from pathlib import Path
    p = Path(path)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
        return f"Written {len(content)} bytes to {path}"
    except Exception as exc:
        return f"Error writing {path}: {exc}"


@langchain_tool
def filesystem_list(path: str = ".") -> str:
    """List files and directories at a path.

    Args:
        path: Directory path to list (defaults to current directory).
    """
    from pathlib import Path
    p = Path(path)
    if not p.exists():
        return f"Error: path not found: {path}"
    try:
        entries = sorted(p.iterdir(), key=lambda x: (not x.is_dir(), x.name))
        lines = []
        for entry in entries:
            suffix = "/" if entry.is_dir() else ""
            lines.append(f"  {entry.name}{suffix}")
        return "\n".join(lines) if lines else "(empty)"
    except Exception as exc:
        return f"Error listing {path}: {exc}"


FILESYSTEM_TOOLS = [filesystem_read, filesystem_write, filesystem_list]
