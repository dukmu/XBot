"""Behavioral guarantees for single-runtime session ownership."""

import os
import subprocess
import sys
import threading
from pathlib import Path

from XBotv2.core.filesystem.session_lock import acquire_session
from XBotv2.core.errors import OperationError


_REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
_TRY_ACQUIRE = """
import sys
from pathlib import Path
from XBotv2.core.errors import OperationError
from XBotv2.core.filesystem.session_lock import acquire_session

try:
    ownership = acquire_session(Path(sys.argv[1]), label="child")
except OperationError as exc:
    print(exc.code)
    raise SystemExit(3)
ownership.release()
print("acquired")
"""
_DIE_WHILE_OWNING = """
import os
import sys
from pathlib import Path
from XBotv2.core.filesystem.session_lock import acquire_session

acquire_session(Path(sys.argv[1]), label="crashed-runtime")
os._exit(0)
"""


def _child_env() -> dict[str, str]:
    existing = os.environ.get("PYTHONPATH", "")
    python_path = os.pathsep.join(
        part for part in (str(_REPOSITORY_ROOT / "XBotv2"), existing) if part
    )
    return {**os.environ, "PYTHONPATH": python_path}


def _try_acquire(root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", _TRY_ACQUIRE, str(root)],
        cwd=_REPOSITORY_ROOT,
        env=_child_env(),
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )


def test_other_process_cannot_write_an_owned_session(tmp_path):
    root = tmp_path / "session"
    ownership = acquire_session(root, label="parent")
    try:
        refused = _try_acquire(root)
        assert refused.returncode == 3
        assert refused.stdout.strip() == "session_in_use"
    finally:
        ownership.release()

    allowed = _try_acquire(root)
    assert allowed.returncode == 0, allowed.stderr
    assert allowed.stdout.strip() == "acquired"


def test_ownership_does_not_materialize_a_session_directory(tmp_path):
    root = tmp_path / "sessions" / "unused"

    ownership = acquire_session(root, label="empty-session")
    try:
        assert not root.exists()
    finally:
        ownership.release()


def test_runtimes_in_one_process_share_ownership_until_last_close(tmp_path):
    root = tmp_path / "session"
    first = acquire_session(root, label="main")
    second = acquire_session(root, label="child-thread")
    assert first is second
    assert first.count == 2

    first.release()
    assert first.count == 1
    assert _try_acquire(root).returncode == 3

    second.release()
    assert first.count == 0
    allowed = _try_acquire(root)
    assert allowed.returncode == 0, allowed.stderr


def test_concurrent_runtime_starts_share_one_process_lock(tmp_path):
    root = tmp_path / "session"
    acquired = []
    errors = []

    def acquire():
        try:
            acquired.append(acquire_session(root, label="runtime"))
        except BaseException as exc:  # surfaced by assertions below
            errors.append(exc)

    threads = [threading.Thread(target=acquire) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert all(not thread.is_alive() for thread in threads)
    assert errors == []
    assert len(acquired) == 8
    assert len({id(owner) for owner in acquired}) == 1
    owner = acquired[0]
    assert owner.count == 8
    for _ in acquired:
        owner.release()
    assert owner.count == 0


def test_process_exit_releases_its_session_lock(tmp_path):
    root = tmp_path / "session"
    crashed = subprocess.run(
        [sys.executable, "-c", _DIE_WHILE_OWNING, str(root)],
        cwd=_REPOSITORY_ROOT,
        env=_child_env(),
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert crashed.returncode == 0, crashed.stderr

    owner = acquire_session(root, label="after-crash")
    assert owner.count == 1
    owner.release()
