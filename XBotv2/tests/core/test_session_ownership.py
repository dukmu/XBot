"""Behavioral guarantees for single-runtime session ownership."""

import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest
from xcore import ServiceNotFoundError

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


@pytest.mark.asyncio
async def test_failed_child_start_keeps_parent_session_exclusive(tmp_path):
    from XBotv2.application.app import start_application
    from XBotv2.core.paths import RuntimePaths
    from XBotv2.llm.mock import MockLLM

    paths = RuntimePaths.from_data_dir(tmp_path / "data")
    parent = await start_application(
        paths=paths, session_id="owned", thread_id="main",
        workspace_root=tmp_path, no_plugins=True, llm_override=MockLLM(responses=[]),
    )
    try:
        with pytest.raises(ServiceNotFoundError, match="Unknown primary agent: uninstalled-agent"):
            await start_application(
                paths=paths, session_id="owned", thread_id="child",
                workspace_root=tmp_path, no_plugins=True,
                selected_agent="uninstalled-agent", is_subagent=True,
                llm_override=MockLLM(responses=[]),
            )
        refused = _try_acquire(paths.session("owned").root)
        assert refused.returncode == 3, refused.stdout + refused.stderr
        assert refused.stdout.strip() == "session_in_use"
    finally:
        await parent.destroy()
    assert _try_acquire(paths.session("owned").root).returncode == 0


@pytest.mark.asyncio
async def test_refused_start_does_not_remove_the_winning_runtime_files(tmp_path, monkeypatch):
    import XBotv2.application.app as application
    from XBotv2.core.paths import RuntimePaths

    paths = RuntimePaths.from_data_dir(tmp_path / "data")
    root = paths.session("contended").root
    marker = root / "winner.txt"

    def another_runtime_won(session_root, *, label):
        # The other process materializes its session between this caller's
        # initial path lookup and its attempt to acquire the lock.
        root.mkdir(parents=True, exist_ok=True)
        marker.write_text("owned by the other runtime", encoding="utf-8")
        raise OperationError("session_in_use", "another runtime owns it")

    monkeypatch.setattr(application, "acquire_session", another_runtime_won)
    with pytest.raises(OperationError, match="another runtime owns it"):
        await application.start_application(
            paths=paths, session_id="contended", workspace_root=tmp_path,
            no_plugins=True,
        )
    assert marker.read_text(encoding="utf-8") == "owned by the other runtime"


@pytest.mark.asyncio
async def test_persistence_creation_failure_releases_startup_claim(tmp_path, monkeypatch):
    import XBotv2.application.app as application
    from XBotv2.core.paths import RuntimePaths

    paths = RuntimePaths.from_data_dir(tmp_path / "data")

    def fail_create(*args, **kwargs):
        raise OSError("cannot create persistence")

    monkeypatch.setattr(application.ThreadPersistence, "create", fail_create)
    with pytest.raises(OSError, match="cannot create persistence"):
        await application.start_application(
            paths=paths, session_id="failed-create", workspace_root=tmp_path,
            no_plugins=True,
        )
    assert _try_acquire(paths.session("failed-create").root).returncode == 0


@pytest.mark.asyncio
async def test_failed_start_rolls_back_only_its_own_new_thread(tmp_path, monkeypatch):
    import XBotv2.application.app as application
    from XBotv2.core.paths import RuntimePaths

    paths = RuntimePaths.from_data_dir(tmp_path / "data")
    session = paths.session("shared")
    sibling_file = session.thread("sibling").root / "accepted.txt"

    async def fail_boot(**kwargs):
        sibling_file.parent.mkdir(parents=True)
        sibling_file.write_text("another thread's data", encoding="utf-8")
        raise RuntimeError("startup failed after sibling started")

    monkeypatch.setattr(application, "boot_application", fail_boot)
    with pytest.raises(RuntimeError, match="startup failed"):
        await application.start_application(
            paths=paths, session_id="shared", thread_id="failed",
            workspace_root=tmp_path, no_plugins=True,
        )
    assert sibling_file.read_text(encoding="utf-8") == "another thread's data"
    assert not session.thread("failed").root.exists()


@pytest.mark.asyncio
async def test_failed_application_handle_creation_releases_the_runtime(tmp_path):
    from XBotv2.application.app import create_agent_application
    from XBotv2.core.paths import RuntimePaths
    from XBotv2.llm.mock import MockLLM
    from XBotv2.session.contracts import AgentApplicationOptions

    paths = RuntimePaths.from_data_dir(tmp_path / "data")
    paths.config_dir.mkdir(parents=True)
    (paths.config_dir / "plugins.yaml").write_text(
        "- id: usage\n  name: uninstalled_usage_for_handoff_test\n", encoding="utf-8",
    )
    # The loop can mount without usage, but the public Agent application port
    # requires a usage reader. Rejection must close the already-started runtime.
    with pytest.raises(ServiceNotFoundError, match="usage"):
        await create_agent_application(AgentApplicationOptions(
            paths=paths, provider_name=None, session_id="handoff", thread_id="main",
            workspace_root=tmp_path, no_plugins=True,
            model_override=MockLLM(responses=[]),
        ))
    allowed = _try_acquire(paths.session("handoff").root)
    assert allowed.returncode == 0, allowed.stdout + allowed.stderr


@pytest.mark.asyncio
async def test_failed_child_start_record_does_not_leak_the_child_runtime(tmp_path):
    from XBotv2.application.app import start_application
    from XBotv2.application.child import ChildApplications
    from XBotv2.application.contracts import ChildApplicationRequest
    from XBotv2.core.paths import RuntimePaths
    from XBotv2.llm.mock import MockLLM

    (tmp_path / ".agents").mkdir()
    (tmp_path / ".agents" / "reviewer.md").write_text(
        "---\ndescription: Review\nmode: subagent\n---\nReview.", encoding="utf-8",
    )
    paths = RuntimePaths.from_data_dir(tmp_path / "data")
    llm = MockLLM(responses=[])
    parent = await start_application(
        paths=paths, session_id="record-failed", thread_id="main",
        workspace_root=tmp_path, llm_override=llm,
    )

    class FailedLifecycle:
        def append(self, record):
            raise OSError("lifecycle write failed")

    try:
        definition = parent.agent_catalog.get("reviewer")
        assert definition is not None
        children = ChildApplications(
            paths=paths, provider_name=None, session_id="record-failed",
            workspace_root=tmp_path, no_plugins=False, plugin_dirs=[],
            llm_override=llm, parent_thread_id="main", interactive=False,
        )
        with pytest.raises(OSError, match="lifecycle write failed"):
            await children.spawn(ChildApplicationRequest(
                definition=definition, thread_id="child", prompt="Review",
                parent_permissions=parent.permissions, client_events=None,
            ), FailedLifecycle())
        assert _try_acquire(paths.session("record-failed").root).returncode == 3
    finally:
        await parent.destroy()
    allowed = _try_acquire(paths.session("record-failed").root)
    assert allowed.returncode == 0, allowed.stdout + allowed.stderr
