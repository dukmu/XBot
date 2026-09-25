"""Interactions component: live client interaction coordination as a plugin.

Provides ``ctx.interactions`` — the in-memory coordination for model-facing
interaction requests (``ask_user`` input).  The service owns one
``InteractionWaiter`` per engine turn, exposes the submit/cancel/pending
surface the session uses, and routes blocking ``user_input_required``
events through the installed live sink (the protocol) or the waiter.
"""

from __future__ import annotations

from xcore import Context
import uuid
from pydantic import JsonValue

from XBotv2.interactions.interactions import InteractionWaiter
from XBotv2.interactions.contracts import (
    InteractionReceipt,
    InteractionRegistration,
    InteractionResolution,
    InteractionsPort,
    InteractionWaiterPort,
    ResolutionFactory,
)
from XBotv2.agentloop import Events
from XBotv2.agentloop.events import SessionLifecycle
from XBotv2.application.contracts import ApplicationEventsPort, ClientEventsPort
from XBotv2.interactions.tools import build_ask_user_tool, send_message
from XBotv2.interactions.protocol import (
    Answered,
    InputCancelled,
    InputTimedOut,
    UserInputOption,
    UserInputRequest,
    UserInputRecorded,
)


def _user_input_recorded(receipt: InteractionReceipt) -> UserInputRecorded:
    resolution = receipt.resolution
    if not isinstance(resolution, (Answered, InputTimedOut, InputCancelled)):
        raise TypeError(
            f"Invalid user-input resolution: {type(resolution).__name__}"
        )
    return UserInputRecorded(
        interaction_id=receipt.interaction_id,
        resolution=resolution,
        pending_ids=receipt.pending_ids,
    )


def _user_input_timeout(request: object) -> float | None:
    if not isinstance(request, UserInputRequest):
        raise TypeError(f"Invalid user-input request: {type(request).__name__}")
    return request.timeout_seconds


class InteractionsService(InteractionsPort):
    """Per-engine interaction coordination with an installed event sink."""

    def __init__(
        self,
        events: ApplicationEventsPort,
        client_events: ClientEventsPort,
    ) -> None:
        self._events = events
        self._client_events = client_events
        self._waiter = InteractionWaiter(
            timed_out=lambda reason: InputTimedOut(reason=reason),
            cancelled=lambda reason: InputCancelled(reason=reason),
        )

    @property
    def waiter(self) -> InteractionWaiterPort:
        return self._waiter

    def create_waiter(
        self,
        *,
        timed_out: ResolutionFactory,
        cancelled: ResolutionFactory,
    ) -> InteractionWaiterPort:
        return InteractionWaiter(timed_out=timed_out, cancelled=cancelled)

    def session_closed(self, _event: SessionLifecycle) -> None:
        self._waiter.cancel_all("session_closed")

    async def request_user_input(
        self,
        question: str,
        *,
        options: tuple[UserInputOption, ...] = (),
        source: str = "interaction",
        timeout_seconds: float | None = None,
        tool_call_id: str = "",
    ) -> InteractionResolution:
        """Publish and resolve one user-input request owned by this plugin.

        The event travels on the turn stream itself (the tool pipeline yields
        it alongside the tool message). This service only routes it through
        the installed live sink (preferred) or the fallback waiter.
        Without a live sink the request fails closed to ``unsupported`` so
        the turn never hangs on a client that cannot answer.  Returns the
        answered dict (``status=answered`` with the answer, or a closed
        status such as ``disconnected`` / ``timeout`` / ``cancelled`` /
        ``unsupported``).
        """
        request_id = f"user_input:{tool_call_id or uuid.uuid4().hex}"
        payload = UserInputRequest(
            interaction_id=request_id,
            tool_call_id=tool_call_id,
            source=source,
            question=question,
            options=options,
            timeout_seconds=timeout_seconds,
            resume_supported=True,
        )
        sink_result = await self._client_events.request(payload)
        if sink_result is not None:
            return sink_result
        wait_timeout = 0 if timeout_seconds is None else timeout_seconds
        result = await self._waiter.wait(request_id, wait_timeout)
        if isinstance(result, InputTimedOut) and timeout_seconds is None:
            return InputCancelled(reason="live_user_input_unsupported")
        return result


class InteractionsComponent:
    """Register the interactions service as ``ctx.interactions``."""

    inject = ["tools", "client_events", "session_launch"]
    name = "xbot.interactions"

    def apply(
        self, ctx: Context, config: dict[str, JsonValue] | None = None
    ) -> None:
        service = InteractionsService(ctx, ctx.client_events)
        ctx.set("interactions", service)
        ctx.dispose(ctx.client_events.register_interaction(
            InteractionRegistration(
                kind="user_input_required",
                request_type=UserInputRequest,
                resolution_types=(Answered, InputTimedOut, InputCancelled),
                waiter=service.waiter,
                timeout_seconds=_user_input_timeout,
                recorded_event=_user_input_recorded,
            )
        ))
        ctx.tools.register(send_message)
        if ctx.session_launch.interactive:
            ctx.tools.register(build_ask_user_tool(service))
        ctx.on(Events.SESSION_CLOSE, service.session_closed)


plugin = InteractionsComponent()
