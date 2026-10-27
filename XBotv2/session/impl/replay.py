"""One process budget for all event replay windows."""

from collections import OrderedDict, deque


class ReplayPool:
    def __init__(self, max_bytes: int) -> None:
        if max_bytes < 0:
            raise ValueError("Replay process budget must be non-negative")
        self._max_bytes = max_bytes
        self._bytes = 0
        self._next = 0
        self._frames = OrderedDict()
        self._windows = set()
        self._closed = False

    @property
    def stats(self) -> dict[str, int]:
        return {
            "budget_bytes": self._max_bytes,
            "resident_bytes": self._bytes,
            "frames": len(self._frames),
            "windows": len(self._windows),
        }

    def window(self, *, capacity: int, max_bytes: int):
        if self._closed:
            raise RuntimeError("Replay pool is closed")
        if capacity < 1 or max_bytes < 1:
            raise ValueError("Replay window limits must be positive")
        window = ReplayWindow(self, capacity, max_bytes)
        self._windows.add(window)
        return window

    def _remove(self, token):
        window, frame, size = self._frames.pop(token)
        self._bytes -= size
        window._remove(token, size)
        return frame

    def close(self):
        if self._closed:
            return
        self._closed = True
        for window in tuple(self._windows):
            window.close()


class ReplayWindow:
    def __init__(self, pool, capacity, max_bytes):
        self._pool = pool
        self._capacity = capacity
        self._max_bytes = max_bytes
        self._tokens = deque()
        self._bytes = 0
        self._wakeups = set()
        self._closed = False

    @property
    def oldest(self):
        return self._pool._frames[self._tokens[0]][1] if self._tokens else None

    def get(self, index):
        if index < 0 or index >= len(self._tokens):
            return None
        return self._pool._frames[self._tokens[index]][1]

    def append(self, frame, size):
        if self._closed:
            raise RuntimeError("Replay window is closed")
        while self._tokens and (len(self._tokens) >= self._capacity or self._bytes + size > self._max_bytes):
            self._pool._remove(self._tokens[0])
        if size > self._max_bytes or size > self._pool._max_bytes:
            while self._tokens:
                self._pool._remove(self._tokens[0])
            self._wake()
            return
        while self._pool._bytes + size > self._pool._max_bytes:
            self._pool._remove(next(iter(self._pool._frames)))
        self._pool._next += 1
        token = self._pool._next
        self._pool._frames[token] = (self, frame, size)
        self._pool._bytes += size
        self._tokens.append(token)
        self._bytes += size
        self._wake()

    def _remove(self, token, size):
        if self._tokens.popleft() != token:
            raise RuntimeError("Replay window order is inconsistent")
        self._bytes -= size
        self._wake()

    def watch(self, wakeup):
        self._wakeups.add(wakeup)

    def unwatch(self, wakeup):
        self._wakeups.discard(wakeup)

    def _wake(self):
        for wakeup in tuple(self._wakeups):
            if wakeup.empty():
                wakeup.put_nowait(None)

    def close(self):
        if self._closed:
            return
        self._closed = True
        while self._tokens:
            self._pool._remove(self._tokens[0])
        self._wake()
        self._wakeups.clear()
        self._pool._windows.discard(self)
