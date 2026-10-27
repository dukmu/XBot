"""Workspace catalog replay event contracts."""

from dataclasses import dataclass
from XBotv2.session.api.contracts import SessionResourceChanged, SessionResourceRemoved
from XBotv2.workspaces.api.contracts import ArchivedSessionsChanged, WorkspaceOrderChanged, WorkspaceResourceChanged, WorkspaceResourceRemoved


WorkspaceCatalogChange = (
    SessionResourceChanged
    | SessionResourceRemoved
    | WorkspaceResourceChanged
    | WorkspaceResourceRemoved
    | WorkspaceOrderChanged
    | ArchivedSessionsChanged
)


class WorkspaceCursorExpired(LookupError):
    def __init__(self, cursor: int, oldest: int) -> None:
        super().__init__(
            f"Workspace event cursor {cursor} expired; "
            f"oldest available sequence is {oldest}"
        )
        self.cursor = cursor
        self.oldest = oldest


@dataclass(frozen=True, slots=True)
class WorkspaceEventFrame:
    sequence: int
    change: WorkspaceCatalogChange
