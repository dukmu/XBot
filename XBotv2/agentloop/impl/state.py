"""Construction and mutation of the thread's loop runtime."""

from __future__ import annotations
from dataclasses import dataclass
from xcore.api.context import Context
from XBotv2.agentloop.api.state import LOOP_STATE
from XBotv2.core.api.variables import RuntimeVariables
from XBotv2.core.api.messages import ConversationMessage
from XBotv2.agentloop.impl.history import ConversationHistory
from XBotv2.agentloop.impl.metadata import ThreadMetadataState
from XBotv2.session.api.contracts import SessionKey


class LoopRuntimeState:
    """Mutable conversation state owned by the Agent loop.

    Constructing the state provides it on the context as ``loop_state`` (and
    constructs the thread's ``thread_metadata``) for the owning fiber's
    lifetime; nobody rebinds either afterwards. The durable inbox is composed
    separately and handed to the loop driver at construction.

    ``turn_count`` is a lifetime counter with one owner (the loop driver): it
    increments when a turn is accepted and is preserved across compaction and
    history edits, exactly like ``SessionStats``. History mutations never
    recompute it; hydration restores it explicitly from the append-only trace.
    """

    def __init__(
        self,
        ctx: Context,
        *,
        key: SessionKey,
        messages: list[ConversationMessage] | ConversationHistory | None = None,
        turn_count: int = 0,
        resumed: bool = False,
        variables: RuntimeVariables = RuntimeVariables(),
    ) -> None:
        self.variables = variables
        self.history = (
            messages
            if isinstance(messages, ConversationHistory)
            else ConversationHistory(messages or ())
        )
        self.metadata = ThreadMetadataState(
            ctx,
            session_id=key.session_id,
            thread_id=key.thread_id,
        )
        self.session = SessionRuntimeState(key=key, metadata=self.metadata)
        self.turn_count = turn_count
        self.resumed = resumed
        ctx.provide(LOOP_STATE, self)

    @property
    def messages(self) -> ConversationHistory:
        return self.history

    @property
    def turn_count(self) -> int:
        return self.session.turn_count

    @turn_count.setter
    def turn_count(self, value: int) -> None:
        self.session.turn_count = value

    def restore_turn_count(self, count: int) -> None:
        """Restore the lifetime counter from the append-only trace (hydration).

        Only the loop driver increments it; hydration restores it once on a
        resumed session.
        """
        self.turn_count = count

    def restore_resumed(self, resumed: bool) -> None:
        """Record whether this session resumed durable state (hydration)."""
        self.resumed = resumed

    def set_history(self, history: ConversationHistory) -> None:
        self.history = history

    async def replace_messages(self, messages: list[ConversationMessage]) -> None:
        await self.history.replace(messages, operation="replace")

    async def replace_message_range(
        self,
        start: int,
        end: int,
        messages: list[ConversationMessage],
        *,
        operation: str,
        preserve_transcript: bool = False,
    ) -> None:
        await self.history.replace_range(
            start,
            end,
            messages,
            operation=operation,
            preserve_transcript=preserve_transcript,
        )


@dataclass(slots=True)
class SessionRuntimeState:
    """Mutable runtime state for one Agent thread.

    Identity is carried by ``key``. Workspace and effective runtime selection
    are always read through the single thread-metadata owner.
    """

    key: SessionKey
    metadata: ThreadMetadataState
    status: str = "active"
    turn_count: int = 0
    event_cursor: int = 0

    @property
    def session_id(self) -> str:
        return self.key.session_id

    @property
    def thread_id(self) -> str:
        return self.key.thread_id

    @property
    def workspace_root(self) -> str:
        return self.metadata.value.workspace_root
