"""Conversation thread IDs cannot overlap internal session streams."""

import pytest

from XBotv2.core.api.errors import OperationError
from XBotv2.core.api.paths import RuntimePaths
from XBotv2.llm.impl.mock import MockLLM
from XBotv2.main import open_runtime
from XBotv2.session.api.contracts import OpenSession, OpenThread, SESSIONS, THREAD_INFO


async def test_reserved_thread_ids_are_rejected_before_persistence(tmp_path):
    paths = RuntimePaths.from_data_dir(tmp_path / "data")
    root = await open_runtime(paths=paths)
    sessions = root.require(SESSIONS)
    try:
        for mode in ("new", "resume"):
            with pytest.raises(OperationError) as error:
                await sessions.open(OpenSession(
                    "rejected", "__internal", str(tmp_path), None, mode, True,
                    model_override=MockLLM(),
                ))
            assert error.value.code == "invalid_request"
        assert not paths.session("rejected").root.exists()

        main = await sessions.open(OpenSession(
            "allowed", "main", str(tmp_path), None, "new", True,
            model_override=MockLLM(),
        ))
        assert main.require(THREAD_INFO).thread_id == "main"
        with pytest.raises(OperationError) as error:
            await sessions.open_thread(OpenThread(
                "allowed", "__internal", "main", str(tmp_path), None, "new", True,
                model_override=MockLLM(),
            ))
        assert error.value.code == "invalid_request"
        assert not paths.session("allowed").thread("__internal").root.exists()
        child = await sessions.open_thread(OpenThread(
            "allowed", "worker", "main", str(tmp_path), None, "new", True,
            model_override=MockLLM(),
        ))
        assert child.require(THREAD_INFO).thread_id == "worker"
    finally:
        await root.close()
