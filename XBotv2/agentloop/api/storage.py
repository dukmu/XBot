"""Canonical conversation archive, independently of an active loop."""

from pathlib import Path
from typing import Protocol

from xcore.api.services import ServiceKey

from XBotv2.core.api.domain import Cursor
from XBotv2.core.api.history import HistoryPage, TrajectoryRead
from XBotv2.core.api.messages import ConversationMessage
from XBotv2.persistence.api.models import ArtifactPayload


class ConversationArchive(Protocol):
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
    ) -> HistoryPage[ConversationMessage]: ...
    async def trajectory_page(
        self,
        session_id: str,
        thread_id: str,
        *,
        cursor: Cursor | None = None,
        limit: int = 160,
        before: int | None = None,
    ) -> TrajectoryRead: ...
    async def artifact(
        self, session_id: str, thread_id: str, artifact_id: str
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
