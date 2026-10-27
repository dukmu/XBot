"""Catalog resilience through the actual process/session owner."""
import pytest_asyncio
from XBotv2.main import open_runtime
from XBotv2.session.api.contracts import SESSIONS, OpenSession
from XBotv2.llm.impl.mock import MockLLM

import asyncio

import os

import time

import pytest

from XBotv2.core.api.domain import (
    MessageId,
    AgentExecutionLimits,
    GenerationSettings,
    ModelRoute,
    ResolvedModelSelection,
    ResolvedRuntimeSelection,
    StandardGenerationMode,
)

from XBotv2.core.api.metadata import ThreadMetadata

from XBotv2.core.api.messages import HumanInputMessage

from XBotv2.core.api.parts import TextPart

from XBotv2.core.api.paths import RuntimePaths

from XBotv2.persistence.impl.store import ThreadPersistence

from XBotv2.session.api.contracts import SESSION_RESOURCE_REMOVED, SessionNotFound

def _session(tmp_path, session_id: str):
    persistence = ThreadPersistence.open(
        RuntimePaths.from_data_dir(tmp_path).session(session_id),
        thread_id="main",
    )
    persistence.history.append([HumanInputMessage(
        id=MessageId(f"input-{session_id}"),
        parts=(TextPart(text=session_id),),
    )])
    persistence.metadata.save(ThreadMetadata(
        runtime_selection=ResolvedRuntimeSelection(
            agent_name="default",
            prompt="",
            limits=AgentExecutionLimits(),
            enabled_tools=(),
            model=ResolvedModelSelection(
                route=ModelRoute(provider="default", model="test"),
                generation=GenerationSettings(
                    mode=StandardGenerationMode(), max_output_tokens=128,
                ),
                context_window=4096,
            ),
        ),
        workspace_root="/workspace",
    ))
    return persistence

def _corrupt(persistence) -> None:
    path = persistence.history.path
    path.write_text(
        path.read_text(encoding="utf-8") + '{"schema_version":\n',
        encoding="utf-8",
    )

@pytest.mark.asyncio
async def test_list_sessions_keeps_an_unreadable_session_visible(tmp_path, caplog, manager):
    caplog.set_level("WARNING", logger="xbotv2.session")
    _session(tmp_path, "good")
    _corrupt(_session(tmp_path, "broken"))

    summaries = {
        summary.session_id: summary
        for summary in await manager.list_sessions()
    }

    assert set(summaries) == {"good", "broken"}
    assert summaries["good"].unreadable is False
    assert summaries["broken"].unreadable is True
    assert summaries["broken"].thread_count == 1
    assert "session.catalog.unreadable" in caplog.text

@pytest.mark.asyncio
async def test_session_summary_still_reports_an_unreadable_session(tmp_path, manager):
    _corrupt(_session(tmp_path, "broken"))

    with pytest.raises(ValueError, match="Invalid messages.jsonl"):
        await manager.session_summary("broken")

@pytest.mark.asyncio
async def test_delete_session_does_not_read_the_trajectory(tmp_path, manager):
    persistence = _session(tmp_path, "broken")
    _corrupt(persistence)

    await manager.delete_session("broken")

    assert not persistence.paths.session.root.exists()

@pytest.mark.asyncio
async def test_mutating_a_missing_session_still_fails_clearly(tmp_path, manager):

    with pytest.raises(SessionNotFound):
        await manager.delete_session("absent")

@pytest_asyncio.fixture
async def manager(tmp_path):
    root = await open_runtime(paths=RuntimePaths.from_data_dir(tmp_path))
    try:
        yield root.require(SESSIONS)
    finally:
        await root.close()


async def test_unused_open_is_invisible_and_shutdown_removes_it(tmp_path, manager):
    scope = await manager.open(OpenSession("unused", "main", str(tmp_path), None, "new", True, model_override=MockLLM()))
    assert not RuntimePaths.from_data_dir(tmp_path).session("unused").root.exists()
    assert await manager.list_sessions() == ()
    await manager.close_session("unused")
    assert not scope.is_active
    assert not RuntimePaths.from_data_dir(tmp_path).session("unused").root.exists()
    assert await manager.list_sessions() == ()
