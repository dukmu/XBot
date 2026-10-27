"""Small filesystem primitives shared by persistence-owning capabilities."""

from __future__ import annotations

import os
import asyncio
import tempfile
from pathlib import Path


def write_text_atomic(path: Path, content: str) -> None:
    """Atomically replace one UTF-8 text file."""
    directories = []
    parent = path.parent
    while not parent.exists():
        directories.append(parent)
        parent = parent.parent
    directories.append(parent)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        for directory in directories if len(directories) > 1 else [path.parent]:
            descriptor = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def write_bytes_atomic(path: Path, content: bytes) -> None:
    """Atomically replace one binary file."""
    directories = []
    parent = path.parent
    while not parent.exists():
        directories.append(parent)
        parent = parent.parent
    directories.append(parent)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
    )
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        for directory in directories if len(directories) > 1 else [path.parent]:
            descriptor = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


async def run_file_io(function, *args):
    task = asyncio.create_task(asyncio.to_thread(function, *args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
        task.result()
        raise
