"""Event declarations use object identity, independent of their diagnostic name."""

from dataclasses import dataclass
from typing import Any, Generic, Protocol, TypeVar

T = TypeVar("T")


@dataclass(frozen=True, eq=False)
class EventKey(Generic[T]):
    name: str

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name:
            raise ValueError("event name must be a non-empty string")

    def __str__(self) -> str:
        return self.name


class EventEmitter(Protocol):
    """Publish facts without exposing services or listener ownership."""
    async def emit(self, event: EventKey | str, *args: Any) -> None: ...


class EventDispatch(EventEmitter, Protocol):
    """Publish facts and dispatch serial interception hooks."""
    async def serial(self, event: EventKey | str, *args: Any) -> Any: ...


class EventQuery(Protocol):
    """Request the first handled result from an event boundary."""
    async def bail(self, event: EventKey | str, *args: Any) -> Any: ...
