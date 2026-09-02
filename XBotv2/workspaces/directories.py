"""Host directory enumeration for human workspace selection."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class DirectoryNotFound(FileNotFoundError):
    pass


class DirectoryNotReadable(PermissionError):
    pass


class DirectoryEntry(BaseModel):
    name: str = Field(min_length=1)
    path: str = Field(min_length=1)
    hidden: bool
    model_config = ConfigDict(extra="forbid", frozen=True)


class DirectoryListing(BaseModel):
    path: str = Field(min_length=1)
    parent: str | None
    home: str = Field(min_length=1)
    separator: Literal["/", "\\"]
    entries: tuple[DirectoryEntry, ...]
    truncated: bool
    model_config = ConfigDict(extra="forbid", frozen=True)


class DirectoryBrowser:
    """List server-side directories without exposing file contents."""

    def __init__(self, default_path: Path | str, *, limit: int = 500) -> None:
        self._default_path = Path(default_path).expanduser().resolve()
        self._home = Path.home().resolve()
        self._limit = limit

    def list(self, path: str | None = None) -> DirectoryListing:
        requested = Path(path).expanduser() if path else self._default_path
        try:
            target = requested.resolve(strict=True)
        except FileNotFoundError as exc:
            raise DirectoryNotFound(str(requested)) from exc
        except PermissionError as exc:
            raise DirectoryNotReadable(str(requested)) from exc
        if not target.is_dir():
            raise DirectoryNotFound(str(target))
        try:
            children = sorted(
                (child for child in target.iterdir() if child.is_dir()),
                key=lambda child: child.name.casefold(),
            )
        except PermissionError as exc:
            raise DirectoryNotReadable(str(target)) from exc
        entries = tuple(
            DirectoryEntry(
                name=child.name,
                path=str(child.resolve()),
                hidden=child.name.startswith("."),
            )
            for child in children[:self._limit]
        )
        parent = target.parent
        return DirectoryListing(
            path=str(target),
            parent=str(parent) if parent != target else None,
            home=str(self._home),
            separator=os.sep,
            entries=entries,
            truncated=len(children) > self._limit,
        )


__all__ = [
    "DirectoryBrowser",
    "DirectoryEntry",
    "DirectoryListing",
    "DirectoryNotFound",
    "DirectoryNotReadable",
]
