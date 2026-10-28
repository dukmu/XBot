"""Behavioral guarantees for single-runtime session ownership."""

import os
import subprocess
import sys
import threading
from pathlib import Path
import pytest
from xcore.api.errors import ServiceNotFoundError
from XBotv2.persistence.impl.session_lock import acquire_session
from XBotv2.core.api.errors import OperationError


_REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
_TRY_ACQUIRE = """
import sys
from pathlib import Path
from XBotv2.core.api.errors import OperationError
from XBotv2.persistence.impl.session_lock import acquire_session

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
from XBotv2.persistence.impl.session_lock import acquire_session

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


def test_independent_claims_in_one_process_conflict(tmp_path):
    root = tmp_path / "session"
    first = acquire_session(root, label="first-runtime")
    try:
        with pytest.raises(OperationError, match="already owned"):
            acquire_session(root, label="second-runtime")
        assert _try_acquire(root).returncode == 3
    finally:
        first.release()
    second = acquire_session(root, label="second-runtime")
    second.release()



def test_concurrent_claims_have_one_runtime_owner(tmp_path):
    root = tmp_path / "session"
    acquired, errors = [], []
    def acquire():
        try:
            acquired.append(acquire_session(root, label="runtime"))
        except OperationError as error:
            errors.append(error.code)
    threads = [threading.Thread(target=acquire) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)
    try:
        assert all(not thread.is_alive() for thread in threads)
        assert len(acquired) == 1
        assert errors == ["session_in_use"] * 7
    finally:
        for owner in acquired:
            owner.release()



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
    owner.release()


async def test_failed_child_start_keeps_parent_session_exclusive(tmp_path):
    from XBotv2.main import open_runtime
    from XBotv2.core.api.paths import RuntimePaths
    from XBotv2.llm.impl.mock import MockLLM
    from XBotv2.session.api.contracts import SESSIONS, OpenSession, OpenThread
    from XBotv2.loader.api.contracts import LoadError
    paths = RuntimePaths.from_data_dir(tmp_path / "data")
    root = await open_runtime(paths=paths)
    sessions = root.require(SESSIONS)
    try:
        parent = await sessions.open(OpenSession("owned", "main", str(tmp_path), None, "new", True, model_override=MockLLM(responses=[])))
        with pytest.raises(LoadError, match="Unknown primary agent: uninstalled-agent"):
            await sessions.open_thread(OpenThread(session_id="owned", thread_id="child-1", parent_thread_id="main", workspace_root=str(tmp_path), provider_name=None, mode="new", no_plugins=True, selected_agent="uninstalled-agent", model_override=MockLLM(responses=[])))
        assert await sessions.locate("owned", "main") is parent
        assert _try_acquire(paths.session("owned").root).returncode == 3
    finally:
        await root.close()
    assert _try_acquire(paths.session("owned").root).returncode == 0
