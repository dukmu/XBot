"""Single-runtime ownership of one persisted session.

Writers are exclusive: one runtime owns a session for as long as it runs, so a
second runtime fails fast instead of interleaving turns into one trajectory.
Readers stay allowed: they never take this lock, and a record becomes visible
only once its terminating newline is durable. The lock inode lives outside the
session directory so an unused session can be discarded without unlinking a
lock that another process may be opening.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from XBotv2.core.api.errors import OperationError

try:
    import fcntl
except ImportError:  # pragma: no cover - POSIX advisory locks
    fcntl = None  # type: ignore[assignment]

_LOCKS_DIRECTORY = ".locks"
_SESSION_LOCK_DIRECTORY = "sessions"


class SessionOwnership:
    """One process's exclusive ownership of one session directory.

    The session owner holds this claim while its thread scopes remain alive.
    Independent runtimes always obtain independent descriptors and conflict,
    even when they run in the same process.
    """

    def __init__(self, root: Path, descriptor: int, label: str) -> None:
        self.root = root
        self.label = label
        self._descriptor = descriptor
        self._released = False

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        fcntl.flock(self._descriptor, fcntl.LOCK_UN)
        os.close(self._descriptor)


def acquire_session(root: Path, *, label: str) -> SessionOwnership:
    """Acquire one independent runtime claim; children reuse their session owner."""
    return _lock_session(Path(root).resolve(), label)


def _session_lock_path(root: Path) -> Path:
    """Stable lock inode, independent of the session's materialized files."""
    return (
        root.parent.parent
        / _LOCKS_DIRECTORY
        / _SESSION_LOCK_DIRECTORY
        / f"{root.name}.lock"
    )


def _lock_session(root: Path, label: str) -> SessionOwnership:
    if fcntl is None:
        raise OperationError(
            "session_locking_unavailable",
            "Session ownership requires POSIX advisory locks (fcntl), which "
            "this platform does not provide",
        )
    path = _session_lock_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
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


