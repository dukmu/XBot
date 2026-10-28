"""Canonical history survives resume and preserves transcript semantics."""
import pytest

from XBotv2.agentloop.api.state import LOOP_STATE
from XBotv2.agentloop.api.storage import ARCHIVE
from XBotv2.core.api.messages import CompactionSummaryMessage, HumanInputMessage
from XBotv2.core.api.parts import TextPart
from XBotv2.core.api.paths import RuntimePaths
from XBotv2.llm.impl.mock import MockLLM
from XBotv2.tests.runtime import thread_scope


def human(identity):
    return HumanInputMessage(id=identity, parts=(TextPart(text=identity),))


@pytest.mark.parametrize("preserve", [True, False])
async def test_history_replacement_and_resume(tmp_path, preserve):
    paths = RuntimePaths.from_data_dir(tmp_path / "data")
    async def open_scope(mode):
        return await thread_scope(paths=paths, session_id="s1", llm_override=MockLLM(), mode=mode)

    scope = await open_scope("new")
    try:
        history = scope.require(LOOP_STATE).history
        await history.extend((human("one"), human("two")))
        with pytest.raises(ValueError, match="identit"):
            await history.append(human("one"))
        await history.replace_range(0, 2, (CompactionSummaryMessage(id="summary", summary="summary"),),
                                    operation="compact" if preserve else "clear", preserve_transcript=preserve)
        assert [m.id for m in await scope.root.require(ARCHIVE).surface("s1", "main")] == ["summary"]
    finally:
        await scope.root.close()
    scope = await open_scope("resume")
    try:
        assert [m.id for m in scope.require(LOOP_STATE).history] == ["summary"]
        page = await scope.root.require(ARCHIVE).message_page("s1", "main")
        assert [m.id for m in page.items] == (["one", "two"] if preserve else ["summary"])
    finally:
        await scope.root.close()
