"""Typed operation contracts routed through an XCore event bus."""

from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Callable
from typing import Generic, Protocol, TypeVar

from XBotv2.core.errors import OperationError

RequestT = TypeVar("RequestT")
ResponseT = TypeVar("ResponseT")
ScopeT = TypeVar("ScopeT")


@dataclass(frozen=True, slots=True)
class EmptyRequest:
    """Explicit payload for query operations without arguments."""


@dataclass(frozen=True, slots=True)
class Operation(Generic[RequestT, ResponseT]):
    """An event name plus its runtime request/response types."""

    name: str
    request_type: type[RequestT]
    response_type: type[ResponseT]
    exclusive: bool | Callable[[RequestT], bool] = False

    def requires_exclusive(self, request: RequestT) -> bool:
        rule = self.exclusive
        return bool(rule(request)) if callable(rule) else rule


@dataclass(frozen=True, slots=True)
class ScopedOperation(Generic[ScopeT, RequestT, ResponseT]):
    """An operation whose owner needs a host-provided execution scope."""

    name: str
    scope_type: type[ScopeT]
    request_type: type[RequestT]
    response_type: type[ResponseT]


class OperationContext(Protocol):
    async def bail(self, event: str, *args: object) -> object: ...


async def dispatch_operation(
    ctx: OperationContext,
    operation: Operation[RequestT, ResponseT],
    request: RequestT,
) -> ResponseT:
    """Dispatch and validate one typed operation or fail at the boundary."""
    if not isinstance(request, operation.request_type):
        raise TypeError(
            f"{operation.name} requires {operation.request_type.__name__} request"
        )
    result = await ctx.bail(operation.name, request)
    if result is None or result is False:
        raise OperationError(
            "capability_unavailable",
            f"No capability handles operation {operation.name!r}",
        )
    if not isinstance(result, operation.response_type):
        raise TypeError(
            f"{operation.name} returned {type(result).__name__}; "
            f"expected {operation.response_type.__name__}"
        )
    return result


async def dispatch_scoped_operation(
    ctx: OperationContext,
    operation: ScopedOperation[ScopeT, RequestT, ResponseT],
    scope: ScopeT,
    request: RequestT,
) -> ResponseT:
    """Dispatch an operation with a typed host-owned execution scope."""
    if not isinstance(scope, operation.scope_type):
        raise TypeError(
            f"{operation.name} requires {operation.scope_type.__name__} scope"
        )
    if not isinstance(request, operation.request_type):
        raise TypeError(
            f"{operation.name} requires {operation.request_type.__name__} request"
        )
    result = await ctx.bail(operation.name, scope, request)
    if result is None or result is False:
        raise OperationError(
            "capability_unavailable",
            f"No capability handles operation {operation.name!r}",
        )
    if not isinstance(result, operation.response_type):
        raise TypeError(
            f"{operation.name} returned {type(result).__name__}; "
            f"expected {operation.response_type.__name__}"
        )
    return result


__all__ = [
    "EmptyRequest",
    "Operation",
    "OperationContext",
    "ScopedOperation",
    "dispatch_operation",
    "dispatch_scoped_operation",
]
