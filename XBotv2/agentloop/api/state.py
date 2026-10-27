"""State capabilities used by Agent selection, history editing and projections."""

from __future__ import annotations
from typing import TYPE_CHECKING, Protocol
from xcore.api.services import ServiceKey
from XBotv2.core.api.history import History
from XBotv2.core.api.metadata import Metadata
from XBotv2.core.api.variables import RuntimeVariables
from XBotv2.core.api.messages import ConversationMessage

if TYPE_CHECKING:
    from XBotv2.session.api.contracts import SessionKey


class ThreadExecution(Protocol):
    key: SessionKey
    status: str
    turn_count: int
    event_cursor: int

    @property
    def session_id(self) -> str: ...
    @property
    def thread_id(self) -> str: ...
    @property
    def workspace_root(self) -> str: ...


class LoopState(Protocol):
    """One authoritative working set; construction belongs to the loop plugin."""

    history: History
    metadata: Metadata
    session: ThreadExecution
    variables: RuntimeVariables
    resumed: bool

    @property
    def messages(self) -> History: ...
    @property
    def turn_count(self) -> int: ...
    async def replace_message_range(
        self,
        start: int,
        end: int,
        messages: list[ConversationMessage],
        *,
        operation: str,
        preserve_transcript: bool = False,
    ) -> None: ...


LOOP_STATE: ServiceKey[LoopState] = ServiceKey("loop_state")
