"""Thread state is independent across threads in one session."""
from XBotv2.agentloop.api.state import LOOP_STATE
from XBotv2.core.api.messages import HumanInputMessage
from XBotv2.core.api.parts import TextPart
from XBotv2.core.api.paths import RuntimePaths
from XBotv2.llm.impl.mock import MockLLM
from XBotv2.session.api.contracts import OpenThread, SESSIONS
from XBotv2.tests.runtime import thread_scope


async def test_threads_keep_independent_history(tmp_path):
    paths = RuntimePaths.from_data_dir(tmp_path / "data")
    first = await thread_scope(paths=paths, session_id="s1", llm_override=MockLLM())
    try:
        await first.require(LOOP_STATE).history.append(HumanInputMessage(id="one", parts=(TextPart(text="first"),)))
        second = await first.root.require(SESSIONS).open_thread(OpenThread(
            session_id="s1", thread_id="second", parent_thread_id="main",
            mode="new", model_override=MockLLM(), workspace_root=None, provider_name=None, no_plugins=False,
        ))
        assert tuple(second.require(LOOP_STATE).history) == ()
        assert [m.id for m in first.require(LOOP_STATE).history] == ["one"]
    finally:
        await first.root.close()
