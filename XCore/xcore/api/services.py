"""Canonical, typed service identities; values live only in ServiceStore."""

from dataclasses import dataclass
from typing import Generic, TypeVar

T = TypeVar("T")


@dataclass(frozen=True)
class ServiceKey(Generic[T]):
    name: str

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name:
            raise ValueError("service name must be a non-empty string")

    def __str__(self) -> str:
        return self.name


def service_name(key: ServiceKey | str) -> str:
    if isinstance(key, ServiceKey):
        return key.name
    if not isinstance(key, str) or not key:
        raise ValueError("service name must be a non-empty string or ServiceKey")
    return key
