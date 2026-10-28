"""Process-wide byte-bounded cache for immutable physical rollout records."""
from __future__ import annotations

from collections import OrderedDict
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path


@dataclass(slots=True)
class _Entry:
    data: bytes
    charge: int
    pins: int = 0
    retired: bool = False


class RecordCache:
    def __init__(self, budget: int):
        self.budget = budget
        self._entries = OrderedDict()
        self._versions = {}
        self._counts = {}
        self._bytes = 0
        self._pins = 0
        self._pinned_bytes = 0
        self._hits = 0
        self._misses = 0
        self._evictions = 0
        self._bypasses = 0

    @property
    def stats(self):
        return {
            "budget_bytes": self.budget, "resident_bytes": self._bytes,
            "entries": len(self._entries), "pins": self._pins,
            "pinned_bytes": self._pinned_bytes, "hits": self._hits,
            "misses": self._misses, "evictions": self._evictions,
            "bypasses": self._bypasses,
        }

    def observe(self, path, version):
        previous = self._versions.get(path)
        if previous is not None and previous != version:
            self.invalidate(path)

    def advance(self, path, previous, current):
        known = self._versions.get(path)
        if known is None:
            return
        if known == previous:
            self._versions[path] = current
        else:
            self.invalidate(path)

    def _remove(self, key, *, evicted=False):
        entry = self._entries.pop(key)
        path = key[1]
        self._counts[path] -= 1
        if not self._counts[path]:
            del self._counts[path]
            self._versions.pop(path, None)
        if entry.pins:
            entry.retired = True
        else:
            self._bytes -= entry.charge
        if evicted:
            self._evictions += 1

    def invalidate(self, path):
        for key in tuple(self._entries):
            if key[1] == path:
                self._remove(key)

    def drop_session(self, session_id):
        for key in tuple(self._entries):
            if key[0] == session_id:
                self._remove(key)

    def drop_root(self, root):
        root = Path(root)
        for key in tuple(self._entries):
            if Path(key[1]).is_relative_to(root):
                self._remove(key)

    def clear(self):
        for key in tuple(self._entries):
            self._remove(key)

    def lookup(self, key):
        entry = self._entries.get(key)
        if entry is None:
            self._misses += 1
            return None
        self._hits += 1
        self._entries.move_to_end(key)
        return entry.data

    @contextmanager
    def hold(self, key, version, data):
        entry = self._entries.get(key)
        if entry is None:
            # Include byte object, key strings/integers, mapping nodes and entry
            # bookkeeping; charge more than their payload alone.
            charge = (
                len(data) + len(str(key[1]).encode("utf-8"))
                + len(key[0].encode("utf-8")) + len(key[4].encode("utf-8")) + 512
            )
            if charge <= self.budget:
                while self._bytes + charge > self.budget:
                    candidate = next(
                        (name for name, value in self._entries.items() if not value.pins),
                        None,
                    )
                    if candidate is None:
                        break
                    self._remove(candidate, evicted=True)
                if self._bytes + charge <= self.budget:
                    entry = _Entry(data, charge)
                    self._entries[key] = entry
                    self._versions[key[1]] = version
                    self._counts[key[1]] = self._counts.get(key[1], 0) + 1
                    self._bytes += charge
            if entry is None:
                self._bypasses += 1
                yield data
                return
        else:
            self._entries.move_to_end(key)
        entry.pins += 1
        self._pins += 1
        if entry.pins == 1:
            self._pinned_bytes += entry.charge
        try:
            yield entry.data
        finally:
            entry.pins -= 1
            self._pins -= 1
            if not entry.pins:
                self._pinned_bytes -= entry.charge
                if entry.retired:
                    self._bytes -= entry.charge
