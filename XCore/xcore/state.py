"""StateService: shared JSON state with explicit file or memory storage.

XCore's first-class answer to "recoverable state" (a Cordis extension: the JS
framework persists via database/config, not a KV state service).  Backed by a
versioned JSON-lines log of key changes. Restart replays complete records;
only an unterminated tail following a valid header is uncommitted.
``StateService.memory()`` instead keeps values only for its lifetime.

Design notes (from the design review, E1/E2):

- One shared in-memory cache + one ``asyncio.Lock`` per service. All views --
  including ``namespace()`` prefixes -- operate on the shared cache, so
  concurrent writes from different namespaces never lose keys.
- ``ctx.state`` registers itself as the root service ``"state"``, so plugins
  may ``inject: ["state"]`` and read it via ``ctx.state`` / ``ctx.get``.
- Corrupt files raise ``RuntimeError`` (fail loudly, never silently recover).
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


logger = logging.getLogger("xcore.state")


def _validate_jsonable(value: Any) -> None:
    """Reject values that cannot round-trip through JSON (UTF-8, no NaN)."""
    json.dumps(value, ensure_ascii=False, allow_nan=False)


@dataclass(frozen=True)
class _FileStorage:
    path: Path

    async def load(self) -> dict[str, Any]:
        path = self.path
        data: dict[str, Any] = {}
        if not path.exists():
            return data
        try:
            with path.open("rb") as stream:
                header = stream.readline()
                if not header.endswith(b"\n"):
                    raise ValueError("missing complete state header")
                record = json.loads(header)
                if record != {"version": 1} or type(record["version"]) is not int:
                    raise ValueError("unsupported state format")
                for line in stream:
                    if not line.endswith(b"\n"):
                        break
                    record = json.loads(line)
                    _validate_jsonable(record)
                    _apply_change(data, record)
        except (ValueError, TypeError, KeyError) as exc:
            raise RuntimeError(f"state file {path} is corrupted or unsupported") from exc
        except OSError as exc:
            raise RuntimeError(f"cannot read state file {path}: {exc}") from exc
        return data

    async def append(self, change: dict[str, Any]) -> None:
        path = self.path
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = (json.dumps(change, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")
        existed = path.exists()
        descriptor = os.open(path, os.O_CREAT | os.O_APPEND | os.O_RDWR, 0o600)
        offset = os.fstat(descriptor).st_size
        try:
            # Complete lines are committed; discard only an interrupted tail.
            end = offset
            while offset:
                length = min(offset, 4096)
                offset -= length
                os.lseek(descriptor, offset, os.SEEK_SET)
                newline = os.read(descriptor, length).rfind(b"\n")
                if newline >= 0:
                    offset += newline + 1
                    break
            if offset != end:
                os.ftruncate(descriptor, offset)
            if offset == 0:
                payload = b'{"version":1}\n' + payload
            try:
                remaining = memoryview(payload)
                while remaining:
                    written = os.write(descriptor, remaining)
                    if written == 0:
                        raise OSError("State append made no progress")
                    remaining = remaining[written:]
                os.fsync(descriptor)
            except BaseException:
                os.ftruncate(descriptor, offset)
                os.fsync(descriptor)
                if not existed:
                    path.unlink()
                raise
        finally:
            os.close(descriptor)


@dataclass(frozen=True)
class _MemoryStorage:
    async def load(self) -> dict[str, Any]:
        return {}

    async def append(self, change: dict[str, Any]) -> None:
        pass


def _apply_change(data: dict[str, Any], change: dict[str, Any]) -> None:
    if not isinstance(change, dict):
        raise ValueError("state change must be an object")
    operation = change.get("op")
    if operation == "clear":
        if set(change) != {"op", "prefix"} or not isinstance(change["prefix"], str):
            raise ValueError("invalid state clear")
        for key in tuple(data):
            if key.startswith(change["prefix"]):
                del data[key]
    elif operation in {"set", "delete"}:
        fields = {"op", "key", "value"} if operation == "set" else {"op", "key"}
        if set(change) != fields or not isinstance(change["key"], str) or not change["key"]:
            raise ValueError("invalid state key change")
        if operation == "set":
            data[change["key"]] = change["value"]
        else:
            del data[change["key"]]
    else:
        raise ValueError("unknown state operation")


@dataclass
class _Shared:
    """One cache and lock shared by every namespace of a state service."""

    storage: _FileStorage | _MemoryStorage
    data: dict[str, Any] | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class StateService:
    """JSON state with namespace views and an explicit storage lifetime.

    File-backed construction appends key changes. ``memory()`` retains the
    same state semantics for one service lifetime without filesystem access.
    """

    def __init__(self, *, path: Path | str) -> None:
        self._shared = _Shared(storage=_FileStorage(Path(path)))
        self._prefix = ""

    @classmethod
    def memory(cls) -> "StateService":
        """Create non-durable state; namespace views share its cache and lock."""
        service = object.__new__(cls)
        service._shared = _Shared(storage=_MemoryStorage())
        service._prefix = ""
        return service

    def _full_key(self, key: str) -> str:
        if not isinstance(key, str) or not key:
            raise ValueError("state key must be a non-empty string")
        return f"{self._prefix}{key}"

    async def _ensure_loaded(self) -> dict[str, Any]:
        if self._shared.data is None:
            self._shared.data = await self._shared.storage.load()
            logger.debug(
                "state.loaded storage=%s keys=%d",
                self._shared.storage,
                len(self._shared.data),
            )
        return self._shared.data

    async def _persist(self, change: dict[str, Any]) -> None:
        await self._shared.storage.append(change)

    # -- public API ---------------------------------------------------------

    async def get(self, key: str, default: Any = None) -> Any:
        """Read one key (``default`` when absent). Loads the file lazily.

        Returns a deep copy: mutating the result never mutates stored state
        (plugins must use ``set`` to persist changes).
        """
        async with self._shared.lock:
            data = await self._ensure_loaded()
            value = data.get(self._full_key(key), default)
            return copy.deepcopy(value)

    async def set(self, key: str, value: Any) -> None:
        """Commit one key to the configured storage. Reject non-JSON values."""
        _validate_jsonable(value)
        full_key = self._full_key(key)
        async with self._shared.lock:
            data = await self._ensure_loaded()
            copied = copy.deepcopy(value)
            await self._persist({"op": "set", "key": full_key, "value": copied})
            data[full_key] = copied
            logger.debug(
                "state.persisted operation=set storage=%s namespace=%s key=%s keys=%d",
                self._shared.storage,
                self._prefix,
                key,
                len(data),
            )

    async def delete(self, key: str) -> None:
        """Remove one key and persist immediately (no-op when absent)."""
        full_key = self._full_key(key)
        async with self._shared.lock:
            data = await self._ensure_loaded()
            if full_key in data:
                await self._persist({"op": "delete", "key": full_key})
                del data[full_key]
                logger.debug(
                    "state.persisted operation=delete storage=%s namespace=%s key=%s keys=%d",
                    self._shared.storage,
                    self._prefix,
                    key,
                    len(data),
                )

    async def clear(self) -> None:
        """Remove every key in this view's namespace and persist."""
        async with self._shared.lock:
            data = await self._ensure_loaded()
            change = {"op": "clear", "prefix": self._prefix}
            await self._persist(change)
            _apply_change(data, change)
            logger.debug(
                "state.persisted operation=clear storage=%s namespace=%s keys=%d",
                self._shared.storage,
                self._prefix,
                len(data),
            )

    async def keys(self) -> list[str]:
        """Snapshot of the keys visible in this view (unprefixed)."""
        async with self._shared.lock:
            data = await self._ensure_loaded()
            if not self._prefix:
                return list(data.keys())
            return [k[len(self._prefix):] for k in data if k.startswith(self._prefix)]

    async def all(self) -> dict[str, Any]:
        """Snapshot (deep-copied) of the key-value pairs in this view."""
        async with self._shared.lock:
            data = await self._ensure_loaded()
            if not self._prefix:
                return copy.deepcopy(data)
            return {
                k[len(self._prefix):]: copy.deepcopy(v)
                for k, v in data.items()
                if k.startswith(self._prefix)
            }

    def namespace(self, prefix: str) -> "StateService":
        """Return a view whose keys are namespaced under ``prefix``.

        Views share the storage, the in-memory cache, and the lock, so concurrent
        writes across namespaces are safe. Used for per-plugin isolation.
        """
        if not isinstance(prefix, str) or not prefix:
            raise ValueError("namespace prefix must be a non-empty string")
        view = object.__new__(StateService)
        view._shared = self._shared
        view._prefix = f"{self._prefix}{prefix}."
        return view


__all__ = ["StateService"]
