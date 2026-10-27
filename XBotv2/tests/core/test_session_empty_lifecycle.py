"""Empty sessions through the production server and session owner."""

import pytest
from XBotv2.main import open_runtime
from XBotv2.core.api.errors import OperationError
from XBotv2.core.api.paths import RuntimePaths
from XBotv2.llm.impl.mock import MockLLM
from XBotv2.session.api.contracts import SessionNotFound
from XBotv2.agentloop.api.storage import ARCHIVE


@pytest.mark.asyncio
async def test_empty_active_trajectory_and_delete(tmp_path):
    paths = RuntimePaths.from_data_dir(tmp_path / "data")
    host = await open_runtime(paths=paths)
    try:
        manager = host.sessions
        await manager.open_session(
            session_id="empty", thread_id="main", provider_name="default",
            workspace_root=str(tmp_path), no_plugins=True, llm_override=MockLLM(),
        )
        thread = paths.session("empty").thread("main")
        assert not thread.rollout_file.exists()
        result = await host.require(ARCHIVE).trajectory_page("empty", "main", cursor=None, limit=10)
        assert result.page.items == ()
        assert result.page.older_cursor is None
        assert result.newest_position == 0
        assert not thread.rollout_file.exists()
        with pytest.raises(OperationError, match="anchor"):
            await host.require(ARCHIVE).trajectory_page("empty", "main", cursor=None, limit=10, before=2)
        with pytest.raises(OperationError, match="cursor"):
            await host.require(ARCHIVE).trajectory_page("empty", "main", cursor="invalid", limit=10)
        with pytest.raises(SessionNotFound):
            await manager.thread_summary("empty", "missing")
        # An empty session may also have an allocated directory.
        paths.session("empty").root.mkdir(parents=True, exist_ok=True)
        await manager.delete_session("empty")
        assert not paths.session("empty").root.exists()
        with pytest.raises(SessionNotFound):
            await manager.delete_session("empty")
    finally:
        await host.close()


@pytest.mark.asyncio
async def test_closing_empty_and_nonempty_sessions(tmp_path):
    paths = RuntimePaths.from_data_dir(tmp_path / "data")
    host = await open_runtime(paths=paths)
    try:
        manager = host.sessions
        for session_id in ("empty", "nonempty"):
            await manager.open_session(
                session_id=session_id, thread_id="main", provider_name="default",
                workspace_root=str(tmp_path), no_plugins=True, llm_override=MockLLM(),
            )
        # A session configuration is durable user state even without a turn.
        config = paths.session("nonempty").config_file
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text("{}", encoding="utf-8")
        await manager.close_session("empty")
        assert not paths.session("empty").root.exists()
        await manager.close_session("nonempty")
        assert config.read_text(encoding="utf-8") == "{}"
        await manager.delete_session("nonempty")
        assert not config.exists()
    finally:
        await host.close()


@pytest.mark.asyncio
async def test_delete_unmaterialized_session(tmp_path):
    paths = RuntimePaths.from_data_dir(tmp_path / "data")
    host = await open_runtime(paths=paths)
    try:
        await host.sessions.open_session(
            session_id="empty", thread_id="main", provider_name="default",
            workspace_root=str(tmp_path), no_plugins=True, llm_override=MockLLM(),
        )
        assert not paths.session("empty").root.exists()
        await host.sessions.delete_session("empty")
        assert not paths.session("empty").root.exists()
    finally:
        await host.close()
