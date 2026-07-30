from __future__ import annotations


class MemoryStore:
    def __init__(self) -> None:
        self._items = {}

    def save(self, task) -> None:
        self._items[task.id] = task

    def all(self):
        return list(self._items.values())
