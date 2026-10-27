"""Read a connected client's snapshot at the current thread event cursor."""
from xcore.api.context import Context
from XBotv2.agentloop.api.state import LOOP_STATE
from XBotv2.agentloop.api.contracts import AGENT_LOOP
from XBotv2.usage.api.service import USAGE
from XBotv2.interactions.api.contracts import CLIENT_EVENTS
from XBotv2.interactions.api.status import status_slots
from XBotv2.core.api.history import HistoryPage
from XBotv2.session.api.contracts import OpenedThread, THREAD_EVENTS
from XBotv2.session.api.records import conversation_replay

async def thread_snapshot(scope: Context) -> OpenedThread:
    cursor = scope.require(THREAD_EVENTS).sequence
    state = scope.require(LOOP_STATE)
    return OpenedThread(
        key=state.session.key, metadata=state.metadata.value,
        usage=scope.require(USAGE).snapshot(), status_slots=await status_slots(scope),
        event_cursor=cursor,
        history=HistoryPage(items=conversation_replay(state.history.snapshot()), older_cursor=None),
        pending_inputs=scope.require(AGENT_LOOP).pending_inputs(),
        pending_interactions=tuple(scope.require(CLIENT_EVENTS).pending_interactions()),
    )
