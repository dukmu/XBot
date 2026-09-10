"""StateService: recoverable persisted key-value storage.

XCore's first-class answer to "recoverable state" (a Cordis extension: the JS
framework persists via database/config, not a KV state service).  Backed by a
single JSON file written atomically (temp file + ``os.replace``), so a crash
never leaves a half-written file and a restart recovers the last persisted
state.

Design notes (from the design review, E1/E2):

- One shared in-memory cache + one ``asyncio.Lock`` per file.  All views --
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
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


logger = logging.getLogger("xcore.state")


def _validate_jsonable(value: Any) -> None:
    """Reject values that cannot round-trip through JSON (UTF-8, no NaN)."""
    json.dumps(value, ensure_ascii=False, allow_nan=False)


@dataclass
class _Shared:
    """Per-file shared state: cache, lock, and write target."""

    path: Path
    data: dict[str, Any] | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class StateService:
    """Persisted key-value store with atomic writes and namespace views.

    All methods are async; reads load lazily on first access, writes persist
    immediately (await the call for durability).
    """

    def __init__(self, *, path: Path | str) -> None:
        self._shared = _Shared(path=Path(path))
        self._prefix = ""

    # -- internal -----------------------------------------------------------

    def _full_key(self, key: str) -> str:
        if not isinstance(key, str) or not key:
            raise ValueError("state key must be a non-empty string")
        return f"{self._prefix}{key}"

    async def _ensure_loaded(self) -> dict[str, Any]:
        if self._shared.data is not None:
            return self._shared.data
        path = self._shared.path
        if path.exists():
            try:
                text = path.read_text(encoding="utf-8")
            except OSError as exc:
                raise RuntimeError(f"cannot read state file {path}: {exc}") from exc
            if not text.strip():
                data: dict[str, Any] = {}
            else:
                try:
                    loaded = json.loads(text)
                except json.JSONDecodeError as exc:
                    raise RuntimeError(
                        f"state file {path} is corrupted: {exc}"
                    ) from exc
                if not isinstance(loaded, dict):
                    raise RuntimeError(
                        f"state file {path} must contain a JSON object"
                    )
                data = loaded
        else:
            data = {}
        self._shared.data = data
        logger.debug(
            "state.loaded path=%s keys=%d",
            path,
            len(data),
        )
        return data

    async def _persist(self, data: dict[str, Any]) -> None:
        path = self._shared.path
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(
            dir=path.parent,
            prefix=f".{path.name}.",
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(data, stream, ensure_ascii=False, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        except BaseException:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
            raise

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
        """Write one key and persist immediately. Rejects non-JSON values."""
        _validate_jsonable(value)
        full_key = self._full_key(key)
        async with self._shared.lock:
            data = await self._ensure_loaded()
            updated = copy.deepcopy(data)
            updated[full_key] = copy.deepcopy(value)
            await self._persist(updated)
            self._shared.data = updated
            logger.debug(
                "state.persisted operation=set path=%s namespace=%s key=%s keys=%d",
                self._shared.path,
                self._prefix,
                key,
                len(updated),
            )

    async def delete(self, key: str) -> None:
        """Remove one key and persist immediately (no-op when absent)."""
        full_key = self._full_key(key)
        async with self._shared.lock:
            data = await self._ensure_loaded()
            if full_key in data:
                updated = copy.deepcopy(data)
                del updated[full_key]
                await self._persist(updated)
                self._shared.data = updated
                logger.debug(
                    "state.persisted operation=delete path=%s namespace=%s key=%s keys=%d",
                    self._shared.path,
                    self._prefix,
                    key,
                    len(updated),
                )

    async def clear(self) -> None:
        """Remove every key in this view's namespace and persist."""
        async with self._shared.lock:
            data = await self._ensure_loaded()
            updated = copy.deepcopy(data)
            if self._prefix:
                affected = [k for k in updated if k.startswith(self._prefix)]
                for key in affected:
                    del updated[key]
            else:
                updated.clear()
            await self._persist(updated)
            self._shared.data = updated
            logger.debug(
                "state.persisted operation=clear path=%s namespace=%s keys=%d",
                self._shared.path,
                self._prefix,
                len(updated),
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

        Views share the file, the in-memory cache, and the lock, so concurrent
        writes across namespaces are safe. Used for per-plugin isolation.
        """
        if not isinstance(prefix, str) or not prefix:
            raise ValueError("namespace prefix must be a non-empty string")
        view = object.__new__(StateService)
        view._shared = self._shared
        view._prefix = f"{self._prefix}{prefix}."
        return view


__all__ = ["StateService"]
