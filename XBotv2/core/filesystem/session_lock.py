"""Single-runtime ownership of one persisted session directory.

Writers are exclusive: one runtime owns a session for as long as it runs, so a
second runtime fails fast instead of interleaving turns into one trajectory.
Readers stay allowed: they never take this lock, and a record becomes visible
only once its terminating newline is durable.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

from XBotv2.core.errors import OperationError

try:
    import fcntl
except ImportError:  # pragma: no cover - POSIX advisory locks
    fcntl = None  # type: ignore[assignment]

_LOCK_FILE_NAME = "session.lock"


class SessionOwnership:
    """One process's exclusive ownership of one session directory.

    Ownership is shared by every runtime the process starts for that session
    (a main thread and its subagent threads), so releasing one of them keeps
    the session owned until the last one closes.
    """

    def __init__(self, root: Path, descriptor: int, label: str) -> None:
        self.root = root
        self.label = label
        self._descriptor = descriptor
        self._count = 1
        self._released = False

    @property
    def count(self) -> int:
        """How many runtimes in this process currently share the ownership."""
        return self._count

    def release(self) -> None:
        """Drop one runtime's claim; the last one unlocks the session."""
        with _guard:
            if self._released:
                return
            self._count -= 1
            if self._count > 0:
                return
            self._released = True
            _owners.pop(self.root, None)
            fcntl.flock(self._descriptor, fcntl.LOCK_UN)
            os.close(self._descriptor)


_owners: dict[Path, SessionOwnership] = {}
_guard = threading.Lock()


def acquire_session(root: Path, *, label: str) -> SessionOwnership:
    """Own ``root`` for this process or fail with a stable error code."""
    resolved = Path(root).resolve()
    with _guard:
        # Opening the lock file is serialized with the registry check: a second
        # flock from this process would conflict with the first even though both
        # would describe the same owner.
        existing = _owners.get(resolved)
        if existing is not None:
            existing._count += 1
            return existing
        ownership = _lock_session(resolved, label)
        _owners[resolved] = ownership
        return ownership


def _session_lock_path(root: Path) -> Path:
    """The lock file one session's runtime ownership is recorded in."""
    return Path(root) / _LOCK_FILE_NAME


def _lock_session(root: Path, label: str) -> SessionOwnership:
    if fcntl is None:
        raise OperationError(
            "session_locking_unavailable",
            "Session ownership requires POSIX advisory locks (fcntl), which "
            "this platform does not provide",
        )
    root.mkdir(parents=True, exist_ok=True)
    path = _session_lock_path(root)
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(descriptor)
        raise OperationError(
            "session_in_use",
            f"Session {root.name!r} is already owned by another runtime"
            f"{_owner_suffix(path)}",
        ) from None
    _write_owner(descriptor, label)
    return SessionOwnership(root, descriptor, label)


def _write_owner(descriptor: int, label: str) -> None:
    payload = json.dumps({
        "pid": os.getpid(),
        "label": label,
        "acquired_at": datetime.now(timezone.utc).isoformat(),
    })
    os.ftruncate(descriptor, 0)
    os.lseek(descriptor, 0, os.SEEK_SET)
    os.write(descriptor, payload.encode("utf-8"))


def _owner_suffix(path: Path) -> str:
    """Describe the recorded owner for a contended session, when readable."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8") or "{}")
    except (OSError, ValueError):
        return ""
    if not isinstance(raw, dict):
        return ""
    pid = raw.get("pid")
    label = raw.get("label")
    if pid is None and label is None:
        return ""
    return f" (pid {pid}, {label})"


__all__ = ["SessionOwnership", "acquire_session"]
