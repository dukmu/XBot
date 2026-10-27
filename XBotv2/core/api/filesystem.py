"""Shared physical filesystem operations and sandbox worker entry point."""

from pathlib import Path
from typing import Literal

from XBotv2.core.filesystem import operations

PathAccess = Literal["read", "write"]
PATH_ACCESS: dict[str, tuple[tuple[str, PathAccess], ...]] = {
    "read": (("path", "read"),),
    "read_bytes": (("path", "read"),),
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

execute = operations.execute
WORKER_PATH = Path(operations.__file__).resolve()

__all__ = ["PATH_ACCESS", "WORKER_PATH", "execute"]
