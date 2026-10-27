"""Archive reads and artifact authorization without opening a thread runtime."""
from __future__ import annotations
from XBotv2.core.api.artifacts import ArtifactKind, ArtifactRef
from XBotv2.core.api.messages import HumanInputMessage, RuntimeNoticeMessage, ToolMessage
from XBotv2.core.api.parts import ImagePart
from XBotv2.core.api.tools import ToolSucceeded, ToolFailed
from XBotv2.core.api.history import HistoryPage, HistoryCursorInvalid
from XBotv2.core.api.errors import OperationError
from XBotv2.persistence.api.models import ArtifactPayload

def _history_artifact(
    messages: tuple[ConversationMessage, ...],
    artifact_id: str,
) -> ArtifactRef | None:
    for message in messages:
        if isinstance(message, (HumanInputMessage, RuntimeNoticeMessage)):
            for artifact in message.artifacts:
                if artifact.id == artifact_id:
                    return artifact
            images = [part.image for part in message.parts if isinstance(part, ImagePart)]
            for image in images:
                if image.artifact_id == artifact_id:
                    return ArtifactRef(
                        id=image.artifact_id,
                        kind=ArtifactKind.MEDIA,
                        media_type=image.media_type,
                        size=image.size,
                    )
        if isinstance(message, ToolMessage):
            outcome = message.outcome
            if isinstance(outcome, (ToolSucceeded, ToolFailed)):
                for image in outcome.output.artifacts:
                    if image.id == artifact_id:
                        return image
    return None

class ArchiveReads:
    def messages(self, session_id, thread_id):
        return tuple(self._reader(session_id, thread_id).history.load_transcript())

    def message_page(self, session_id, thread_id, *, cursor=None, limit=None):
        if limit is None:
            if cursor is not None:
                raise OperationError("invalid_cursor", "A message cursor requires a page limit")
            return HistoryPage(items=self.messages(session_id, thread_id), older_cursor=None)
        try:
            return self._reader(session_id, thread_id).history.page_transcript(limit=limit, cursor=cursor)
        except HistoryCursorInvalid as exc:
            raise OperationError("invalid_cursor", str(exc)) from exc

    def trajectory_page(self, session_id, thread_id, *, cursor=None, limit=100, before=None):
        try:
            return self._reader(session_id, thread_id).history.page_trajectory(limit=limit, cursor=cursor, before=before)
        except HistoryCursorInvalid as exc:
            raise OperationError("invalid_cursor", str(exc)) from exc

    def artifact(self, session_id, thread_id, artifact_id):
        ref = _history_artifact(self.messages(session_id, thread_id), artifact_id)
        if ref is None:
            raise OperationError("artifact_not_found", f"Artifact is not referenced by {session_id}/{thread_id}")
        try:
            content = self.artifacts(self._paths.session(session_id).thread(thread_id)).read(ref)
        except (FileNotFoundError, ValueError) as exc:
            raise OperationError("artifact_not_found", f"Artifact does not exist: {artifact_id}") from exc
        return ArtifactPayload(content, ref.media_type, ref.name)

    def _reader(self, session_id, thread_id):
        return self.thread(self._paths.session(session_id), thread_id=thread_id)
