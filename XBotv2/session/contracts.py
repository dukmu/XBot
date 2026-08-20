"""Public contracts for creating Agent applications from the session host."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Generic, Protocol, TypeVar

from xcore import Context

from XBotv2.core.agents import AgentDefinition
from XBotv2.core.paths import RuntimePaths
from XBotv2.core.providers import BaseProvider
from XBotv2.core.errors import OperationError
from XBotv2.core.operations import Operation, OperationContext


RequestT = TypeVar("RequestT")
ResponseT = TypeVar("ResponseT")
DISPATCH_OPERATION = "session/dispatch"
DISPATCH_SESSION_OPERATION = "session/dispatch-all"
OPERATION_COMPLETED = "session/operation/completed"
PREPARE_FORK = "session/prepare-fork"


@dataclass(frozen=True, slots=True)
class PrepareFork:
    session_id: str
    thread_id: str


@dataclass(frozen=True, slots=True)
class SessionStatus:
    session_id: str
    thread_id: str
    provider: str
    model: str


@dataclass(frozen=True, slots=True)
class SessionRef:
    session_id: str
    thread_id: str


@dataclass(frozen=True, slots=True)
class SessionDispatch(Generic[RequestT, ResponseT]):
    """Root-to-child operation envelope interpreted only by SessionHost."""

    target: SessionRef
    operation: Operation[RequestT, ResponseT]
    request: RequestT


@dataclass(frozen=True, slots=True)
class SessionGroupDispatch(Generic[RequestT, ResponseT]):
    """Root-to-child operation envelope for every active thread in a session."""

    session_id: str
    operation: Operation[RequestT, ResponseT]
    request: RequestT


@dataclass(frozen=True, slots=True)
class SessionOperationCompleted:
    target: SessionRef
    operation_name: str
    result: object


async def dispatch_session_operation(
    ctx: OperationContext,
    target: SessionRef,
    operation: Operation[RequestT, ResponseT],
    request: RequestT,
) -> ResponseT:
    """Route one typed request through root XCore into a session context."""
    if not isinstance(request, operation.request_type):
        raise TypeError(
            f"{operation.name} requires {operation.request_type.__name__} request"
        )
    result = await ctx.bail(
        DISPATCH_OPERATION,
        SessionDispatch(target=target, operation=operation, request=request),
    )
    if result is None or result is False:
        raise OperationError(
            "session_host_unavailable",
            "No session host handles typed operation dispatch",
        )
    if not isinstance(result, operation.response_type):
        raise TypeError(
            f"{operation.name} returned {type(result).__name__}; "
            f"expected {operation.response_type.__name__}"
        )
    return result


async def dispatch_session_group_operation(
    ctx: OperationContext,
    session_id: str,
    operation: Operation[RequestT, ResponseT],
    request: RequestT,
) -> tuple[ResponseT, ...]:
    """Route one typed request to every active thread in a session."""
    if not isinstance(request, operation.request_type):
        raise TypeError(
            f"{operation.name} requires {operation.request_type.__name__} request"
        )
    result = await ctx.bail(
        DISPATCH_SESSION_OPERATION,
        SessionGroupDispatch(
            session_id=session_id,
            operation=operation,
            request=request,
        ),
    )
    if result is None or result is False:
        raise OperationError(
            "session_host_unavailable",
            "No session host handles typed session operation dispatch",
        )
    if not isinstance(result, tuple) or any(
        not isinstance(item, operation.response_type) for item in result
    ):
        raise TypeError(
            f"{operation.name} session dispatch returned an invalid response"
        )
    return result


@dataclass(frozen=True, slots=True)
class AgentApplicationOptions:
    """Launch facts for one session-owned Agent application."""

    paths: RuntimePaths
    provider_name: str
    session_id: str
    thread_id: str
    workspace_root: Path
    no_plugins: bool
    plugin_configs: dict[str, dict[str, object]] | None = None
    model_override: BaseProvider | None = None
    selected_agent: str | None = None
    agent_definition: AgentDefinition | None = None
    parent_thread_id: str = ""
    parent_permission_system: object | None = None
    is_subagent: bool = False
    interactive: bool = True


class AgentApplicationFactory(Protocol):
    """Composition-owned factory consumed by the session host."""

    async def __call__(self, options: AgentApplicationOptions) -> Context: ...


__all__ = [
    "AgentApplicationFactory",
    "AgentApplicationOptions",
    "DISPATCH_OPERATION",
    "DISPATCH_SESSION_OPERATION",
    "OPERATION_COMPLETED",
    "PREPARE_FORK",
    "PrepareFork",
    "SessionDispatch",
    "SessionGroupDispatch",
    "SessionOperationCompleted",
    "SessionRef",
    "SessionStatus",
    "dispatch_session_operation",
    "dispatch_session_group_operation",
]
