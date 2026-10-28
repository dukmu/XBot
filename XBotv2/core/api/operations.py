"""Typed operation contracts routed through an XCore event bus."""

from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Callable
from typing import Generic, TypeVar
from xcore.api.events import EventQuery
from XBotv2.core.api.errors import OperationError

RequestT = TypeVar("RequestT")
ResponseT = TypeVar("ResponseT")


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


async def dispatch_operation(
    ctx: EventQuery,
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
