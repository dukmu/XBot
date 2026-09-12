"""Single-owner runtime ownership of one session directory."""

import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from XBotv2.application.app import start_application
from XBotv2.core.errors import OperationError
from XBotv2.core.filesystem.session_lock import acquire_session
from XBotv2.core.messages import Message
from XBotv2.core.paths import RuntimePaths
from XBotv2.llm.mock import MockLLM
from XBotv2.persistence.store import ThreadPersistence

_REPOSITORY_ROOT = Path(__file__).resolve().parents[3]

_HOLD_OWNERSHIP = """
import sys
import time
from pathlib import Path
from XBotv2.core.filesystem.session_lock import acquire_session

root, marker, seconds = sys.argv[1:4]
acquire_session(Path(root), label="holder")
Path(marker).write_text("owned", encoding="utf-8")
time.sleep(float(seconds))
"""

_DIE_WITH_OWNERSHIP = """
import os
import sys
from pathlib import Path
from XBotv2.core.filesystem.session_lock import acquire_session

root, marker = sys.argv[1:3]
acquire_session(Path(root), label="crash")
Path(marker).write_text("owned", encoding="utf-8")
os._exit(0)
"""

_TRY_OWNERSHIP = """
import sys
from pathlib import Path
from XBotv2.core.errors import OperationError
from XBotv2.core.filesystem.session_lock import acquire_session

try:
    acquire_session(Path(sys.argv[1]), label="second")
except OperationError as exc:
    print(exc.code)
    raise SystemExit(3)
print("acquired")
"""

_READ_HISTORY = """
import sys
from XBotv2.core.paths import RuntimePaths
from XBotv2.persistence.store import ThreadPersistence

data_dir, session_id, thread_id = sys.argv[1:4]
paths = RuntimePaths.from_data_dir(data_dir).session(session_id)
history = ThreadPersistence.open(paths, thread_id=thread_id).history
print("|".join(message.content for message in history.load()))
"""


def _child_env() -> dict[str, str]:
    existing = os.environ.get("PYTHONPATH", "")
    python_path = os.pathsep.join(
        part for part in (str(_REPOSITORY_ROOT), existing) if part
    )
    return {**os.environ, "PYTHONPATH": python_path}


def _spawn(script: str, *arguments: str) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-c", script, *arguments],
        env=_child_env(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _wait_for_file(path: Path, timeout: float = 60.0) -> None:
    deadline = time.monotonic() + timeout
    while not path.exists():
        if time.monotonic() >= deadline:
            raise AssertionError(f"{path} was not created in time")
        time.sleep(0.01)


def _session_root(tmp_path: Path, session_id: str = "s1") -> Path:
    return RuntimePaths.from_data_dir(tmp_path).session(session_id).root


def test_second_process_cannot_own_the_same_session(tmp_path):
    root = _session_root(tmp_path)
    marker = tmp_path / "owned"
    holder = _spawn(_HOLD_OWNERSHIP, str(root), str(marker), "30")
    try:
        _wait_for_file(marker)
        started = time.monotonic()
        with pytest.raises(OperationError) as raised:
            acquire_session(root, label="second")
        elapsed = time.monotonic() - started

        assert raised.value.code == "session_in_use"
        assert str(os.getpid()) != ""
        assert holder.pid is not None
        assert f"pid {holder.pid}" in raised.value.message
        assert elapsed < 5
    finally:
        holder.kill()
        holder.wait(timeout=30)


def test_ownership_is_shared_within_one_process(tmp_path):
    root = _session_root(tmp_path)
    first = acquire_session(root, label="main")
    second = acquire_session(root, label="subagent")
    assert first is second
    assert first.count == 2

    first.release()
    refused = _spawn(_TRY_OWNERSHIP, str(root))
    _stdout, _stderr = refused.communicate(timeout=60)
    assert refused.returncode == 3

    second.release()
    allowed = _spawn(_TRY_OWNERSHIP, str(root))
    stdout, _stderr = allowed.communicate(timeout=60)
    assert allowed.returncode == 0, stdout
    assert stdout.strip() == "acquired"


def test_concurrent_acquisition_in_one_process_is_shared(tmp_path):
    """Threads racing for one session must share it, not fail on their own lock."""
    root = _session_root(tmp_path)
    acquired: list[object] = []
    errors: list[BaseException] = []

    def take() -> None:
        try:
            acquired.append(acquire_session(root, label="thread"))
        except BaseException as exc:  # noqa: BLE001 - reported by the assertion
            errors.append(exc)

    threads = [threading.Thread(target=take) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert len({id(ownership) for ownership in acquired}) == 1
    ownership = acquired[0]
    assert ownership.count == 8
    for _ in range(8):
        ownership.release()
    assert ownership.count == 0


def test_ownership_is_released_when_the_owner_process_dies(tmp_path):
    root = _session_root(tmp_path)
    marker = tmp_path / "owned"
    crashed = _spawn(_DIE_WITH_OWNERSHIP, str(root), str(marker))
    _wait_for_file(marker)
    assert crashed.wait(timeout=60) == 0

    ownership = acquire_session(root, label="after crash")

    assert ownership.count == 1
    ownership.release()


def test_reader_is_allowed_while_the_session_is_owned(tmp_path):
    """Writers are exclusive; readers keep working on the same trajectory."""
    root = _session_root(tmp_path)
    ownership = acquire_session(root, label="writer")
    try:
        persistence = ThreadPersistence.open(
            RuntimePaths.from_data_dir(tmp_path).session("s1"),
            thread_id="t1",
        )
        persistence.history.append([Message(role="user", content="owned write")])

        reader = _spawn(_READ_HISTORY, str(tmp_path), "s1", "t1")
        stdout, stderr = reader.communicate(timeout=60)

        assert reader.returncode == 0, stderr
        assert stdout.strip() == "owned write"
    finally:
        ownership.release()


@pytest.mark.asyncio
async def test_application_start_refuses_an_owned_session(tmp_path):
    root = _session_root(tmp_path)
    marker = tmp_path / "owned"
    holder = _spawn(_HOLD_OWNERSHIP, str(root), str(marker), "30")
    try:
        _wait_for_file(marker)
        with pytest.raises(OperationError) as raised:
            await start_application(
                paths=RuntimePaths.from_data_dir(tmp_path),
                session_id="s1",
                plugin_dirs=[],
                llm_override=MockLLM(responses=[]),
            )
        assert raised.value.code == "session_in_use"
    finally:
        holder.kill()
        holder.wait(timeout=30)


@pytest.mark.asyncio
async def test_application_close_releases_the_session(temp_data_dir):
    context = await start_application(
        paths=RuntimePaths.from_data_dir(temp_data_dir),
        session_id="owned-session",
        plugin_dirs=[],
        llm_override=MockLLM(responses=[]),
    )
    root = _session_root(temp_data_dir, "owned-session")
    try:
        refused = _spawn(_TRY_OWNERSHIP, str(root))
        _stdout, _stderr = refused.communicate(timeout=60)
        assert refused.returncode == 3
    finally:
        await context.destroy()

    allowed = _spawn(_TRY_OWNERSHIP, str(root))
    stdout, stderr = allowed.communicate(timeout=60)
    assert allowed.returncode == 0, (stdout, stderr)
