"""Event declarations use object identity, independent of their diagnostic name."""

from dataclasses import dataclass
from typing import Any, Callable, Generic, Protocol, TypeVar, overload

T = TypeVar("T")

Disposer = Callable[[], bool]


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
    @overload
    async def emit(self, event: EventKey[T], payload: T) -> None: ...
    @overload
    async def emit(self, event: str, *args: Any) -> None: ...


class EventDispatch(EventEmitter, Protocol):
    """Publish facts and dispatch serial interception hooks."""
    @overload
    async def serial(self, event: EventKey[T], payload: T) -> Any: ...
    @overload
    async def serial(self, event: str, *args: Any) -> Any: ...


class EventQuery(Protocol):
    """Request the first handled result from an event boundary."""
    @overload
    async def bail(self, event: EventKey[T], payload: T) -> Any: ...
    @overload
    async def bail(self, event: str, *args: Any) -> Any: ...
