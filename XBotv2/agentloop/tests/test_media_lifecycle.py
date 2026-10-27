"""Media references remain complete across authorization, fork, and exchange."""
from contextlib import aclosing

from XBotv2.agentloop.api.state import LOOP_STATE
from XBotv2.agentloop.api.storage import ARCHIVE
from XBotv2.core.api.artifacts import ArtifactKind
from XBotv2.core.api.domain import ToolCallId, ToolTiming
from XBotv2.core.api.messages import ToolMessage
from XBotv2.core.api.parts import MediaPart
from XBotv2.core.api.paths import RuntimePaths
from XBotv2.core.api.tools import ToolCallRef, ToolOutput, ToolSucceeded
from XBotv2.llm.impl.mock import MockLLM
from XBotv2.session.api.contracts import SESSIONS
from XBotv2.tests.runtime import thread_scope


async def test_tool_media_authorization_fork_resume_and_exchange(tmp_path):
    paths = RuntimePaths.from_data_dir(tmp_path / "data")

    async def opened(mode):
        return await thread_scope(
            paths=paths, session_id="media", mode=mode,
            llm_override=MockLLM([{"content": "done"}]),
            extra_plugins=[{"id": "caption", "config": {"auto": False}}],
        )

    async def assert_artifact(archive, session, ref):
        payload = await archive.artifact(session, "main", ref.id)
        async with aclosing(payload.content):
            assert b"".join([chunk async for chunk in payload.content]) == b"media bytes"

    scope = await opened("new")
    try:
        ref = await scope.artifacts.put(
            ArtifactKind.MEDIA, b"media bytes", media_type="image/png", name="named.png"
        )
        message = ToolMessage(
            id="media-tool", call=ToolCallRef(id=ToolCallId("call"), name="read"),
            outcome=ToolSucceeded(output=ToolOutput(parts=(MediaPart(artifact=ref),))),
            timing=ToolTiming(duration_ms=1),
        )
        await scope.require(LOOP_STATE).history.append(message)
        archive = scope.root.require(ARCHIVE)
        await assert_artifact(archive, "media", ref)
        fork = await scope.root.require(SESSIONS).fork_session("media")
        package = tmp_path / "media.jsonl"
        await archive.export_rollout("media", "main", package)
        imported = await archive.import_rollout(package)
        for session in (fork, imported):
            await assert_artifact(archive, session, ref)
            assert await archive.message(session, "main", "media-tool") == message
    finally:
        await scope.root.close()
    resumed = await opened("resume")
    try:
        archive = resumed.root.require(ARCHIVE)
        await assert_artifact(archive, "media", ref)
        assert await archive.message("media", "main", "media-tool") == message
    finally:
        await resumed.root.close()
