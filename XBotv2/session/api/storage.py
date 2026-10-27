"""Session identity and explicit selection persistence contract."""

from dataclasses import dataclass, field
from typing import Protocol

from pydantic import BaseModel, ConfigDict, JsonValue
from xcore.api.services import ServiceKey


class ThreadFacts(BaseModel):
    """Saved reference and explicit user choices, never a resolved Agent copy."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    agent_ref: str = ""
    provider_override: str | None = None
    model_override: str | None = None
    effort_override: str | None = None
    parent_thread_id: str = ""
    workspace_root: str = ""
    title: str = ""


class ThreadRepository(Protocol):
    async def load(self) -> ThreadFacts | None: ...
    async def remember(self, facts: ThreadFacts) -> None: ...
    async def ensure(self) -> None: ...
    async def persisted(self) -> bool: ...
    async def session_title(self) -> str: ...


THREAD_REPOSITORY: ServiceKey[ThreadRepository] = ServiceKey("thread_repository")

FORK_COPY_STATE = "session/fork-copy-state"
FORK_COPY_THREAD = "session/fork-copy-thread"
FORK_FINALIZE = "session/fork-finalize"


@dataclass(slots=True)
class ForkState:
    source_session_id: str
    target_session_id: str
    thread_id: str = ""
    cut: tuple[int, int] = (0, 0)
    handled: set[str] = field(default_factory=set)
    origins: dict[str, JsonValue] = field(default_factory=dict)
