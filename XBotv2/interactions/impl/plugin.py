"""Interactions component: live client interaction coordination as a plugin.

Provides ``ctx.interactions`` — the in-memory coordination for model-facing
interaction requests (``ask_user`` input).  The service owns one
``InteractionWaiter`` per engine turn, exposes the submit/cancel/pending
surface the session uses, and routes blocking ``user_input_required``
events through the installed live sink (the protocol) or the waiter.
"""

from __future__ import annotations
from XBotv2.session.api.contracts import SESSIONS, THREAD_INFO
from XBotv2.interactions.api.contracts import CLIENT_EVENTS
from XBotv2.interactions.impl.client_http import build_router as build_client_router
from XBotv2.server.api.contracts import contribute_router
from XBotv2.persistence.api.contracts import PERSISTENCE

from XBotv2.agentloop.api.contracts import TOOLS
from xcore.api.context import Context
import uuid
from pydantic import JsonValue
from XBotv2.interactions.impl.interactions import InteractionWaiter
from XBotv2.interactions.impl.router import ClientEventRouter
from XBotv2.interactions.api.contracts import (
    INTERACTIONS,
    InteractionReceipt,
    InteractionRegistration,
    InteractionResolution,
    InteractionsPort,
    InteractionWaiterPort,
    ResolutionFactory,
)
from XBotv2.agentloop.api.events import Events, SessionLifecycle
from XBotv2.interactions.api.contracts import ClientEventsPort
from XBotv2.interactions.impl.tools import build_ask_user_tool, notify_user
from XBotv2.interactions.api.models import (
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
        events: Context,
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
        request_id = f"user_input:{uuid.uuid4().hex}"
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

    inject = [TOOLS, "interactive", SESSIONS, THREAD_INFO]
    name = "xbot.interactions"

    async def apply(
        self, ctx: Context, config: dict[str, JsonValue] | None = None
    ) -> None:
        info = ctx.require(THREAD_INFO)
        parent = None
        if info.parent_thread_id:
            owner = await ctx.require(SESSIONS).locate(info.session_id, info.parent_thread_id)
            parent = owner.require(CLIENT_EVENTS)
        client_events = ClientEventRouter(parent, owner=ctx)
        ctx.set("client_events", client_events)
        service = InteractionsService(ctx, client_events)
        ctx.set(INTERACTIONS, service)
        async def drain():
            service.session_closed(SessionLifecycle())
            await client_events.close()
        ctx.pre_stop(drain)
        ctx.dispose(client_events.register_interaction(
            InteractionRegistration(
                kind="user_input_required",
                request_type=UserInputRequest,
                resolution_types=(Answered, InputTimedOut, InputCancelled),
                waiter=service.waiter,
                timeout_seconds=_user_input_timeout,
                recorded_event=_user_input_recorded,
            )
        ))
        ctx.require(TOOLS).register(notify_user)
        if ctx.interactive:
            ctx.require(TOOLS).register(build_ask_user_tool(service))
        ctx.on(Events.SESSION_CLOSE, service.session_closed)


async def mount_thread(ctx, config):
    await InteractionsComponent().apply(ctx, config)


mount_thread.inject = getattr(InteractionsComponent, "inject", ())
mount_thread.provide = ('client_events', 'interactions')
mount_thread.Config = getattr(InteractionsComponent, "Config", None)


async def mount_client_http(ctx):
    await contribute_router(ctx, owner="xbot.interactions.client-http", router=build_client_router(sessions=ctx.sessions, persistence=ctx.require(PERSISTENCE)))


def mount_process(ctx, config):
    ctx.inject(["server", "sessions", PERSISTENCE], mount_client_http)
mount_process.inject = ()
mount_process.provide = ()
