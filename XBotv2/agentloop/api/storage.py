"""Canonical conversation archive, independently of an active loop."""

from pathlib import Path
from typing import Literal, Protocol
from pydantic import BaseModel, ConfigDict

from xcore.api.services import ServiceKey

from XBotv2.core.api.domain import Cursor, InboxTarget
from XBotv2.core.api.history import HistoryPage, TrajectoryRead
from XBotv2.core.api.messages import ConversationMessage
from XBotv2.core.api.artifacts import ArtifactRef
from XBotv2.persistence.api.models import ArtifactPayload


class ExecutionRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    turn_id: str
    input_ids: tuple[str, ...]
    status: Literal["running", "completed", "failed", "cancelled"]
    final_message_id: str | None = None


class InputRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    input_id: str
    status: Literal["pending", "removed", "discarded", "consumed"]
    source: str | None
    event: str | None
    target: InboxTarget


class ConversationArchive(Protocol):
    async def execution_for_turn(self, session_id: str, thread_id: str, turn_id: str) -> ExecutionRecord | None: ...
    async def execution_for_input(self, session_id: str, thread_id: str, input_id: str) -> ExecutionRecord | None: ...
    async def latest_input(self, session_id: str, thread_id: str, *, source: str | None = None, event: str | None = None) -> InputRecord | None: ...
    async def latest_execution(self, session_id: str, thread_id: str) -> ExecutionRecord | None: ...
    async def message(self, session_id: str, thread_id: str, message_id: str) -> ConversationMessage: ...
    async def flush(self, session_id: str, thread_id: str) -> tuple[int, int, int]: ...
    async def surface(
        self, session_id: str, thread_id: str
    ) -> tuple[ConversationMessage, ...]: ...
    async def turn_count(self, session_id: str, thread_id: str) -> int: ...
    async def message_count(self, session_id: str, thread_id: str) -> int: ...
    async def message_page(
        self,
        session_id: str,
        thread_id: str,
        *,
        cursor: Cursor | None = None,
        limit: int = 160,
        before_id: str | None = None,
        after_id: str | None = None,
    ) -> HistoryPage[ConversationMessage]: ...
    async def display_page(self, session_id: str, thread_id: str, *, cursor: Cursor | None = None, limit: int = 160, before_id: str | None = None, after_id: str | None = None): ...
    async def display_body(self, session_id: str, thread_id: str, message_id: str, *, field: str, offset: int = 0, limit: int = 4096): ...
    async def trajectory_page(
        self,
        session_id: str,
        thread_id: str,
        *,
        cursor: Cursor | None = None,
        limit: int = 160,
        before: int | None = None,
    ) -> TrajectoryRead: ...
    async def record_selection(
        self, session_id: str, thread_id: str, *, after_seq: int = 0,
        through_seq: int | None = None, record_ids: list[str] | None = None,
    ) -> ArtifactRef: ...
    async def artifact(
        self, session_id: str, thread_id: str, artifact_id: str, *, offset: int = 0, limit: int | None = None
    ) -> ArtifactPayload: ...
    async def export_rollout(
        self, session_id: str, thread_id: str, destination: Path
    ) -> Path: ...
    async def import_rollout(
        self,
        source: Path,
        *,
        workspace_root: Path | None = None,
        artifact_source: Path | None = None,
    ) -> str: ...


ARCHIVE: ServiceKey[ConversationArchive] = ServiceKey("conversation_archive")
