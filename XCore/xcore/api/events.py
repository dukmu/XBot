"""Event declarations use object identity, independent of their diagnostic name."""

from dataclasses import dataclass
from typing import Generic, TypeVar

T = TypeVar("T")


@dataclass(frozen=True, eq=False)
class EventKey(Generic[T]):
    name: str

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name:
            raise ValueError("event name must be a non-empty string")

    def __str__(self) -> str:
        return self.name
