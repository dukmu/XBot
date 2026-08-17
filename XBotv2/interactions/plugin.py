"""Interactions component: live client interaction coordination as a plugin.

Provides ``ctx.interactions`` — the in-memory coordination for model-facing
interaction requests (``ask_user`` input).  The service owns one
``InteractionWaiter`` per engine turn, exposes the submit/cancel/pending
surface the session uses, and routes blocking ``user_input_required``
events through an installable live sink (the protocol) or the waiter.
"""

from __future__ import annotations

from typing import Any
import uuid

from XBotv2.interactions.interactions import InteractionResult, InteractionWaiter
from XBotv2.core.events import EventContext, Events


class InteractionsService:
    """Per-engine interaction coordination with an installable event sink."""

    def __init__(self, ctx: Any, client_events: Any) -> None:
        self.ctx = ctx
        self.client_events = client_events
        self._waiter = InteractionWaiter()

    @property
    def waiter(self) -> InteractionWaiter:
        return self._waiter

    # ------------------------------------------------------------------
    # Session-facing API
    # ------------------------------------------------------------------

    def submit_user_input(self, request_id: str, answer: Any) -> InteractionResult:
        return self._waiter.answer(request_id, answer=answer)

    def cancel_user_input(self, request_id: str, reason: str = "cancelled") -> InteractionResult:
        return self._waiter.cancel(request_id, reason)

    def cancel_pending_user_inputs(self, reason: str = "cancelled") -> list[InteractionResult]:
        return self._waiter.cancel_all(reason)

    def pending_user_input_request_ids(self) -> list[str]:
        return self._waiter.pending_request_ids()

    def cancel_all(self, reason: str = "cancelled") -> list[InteractionResult]:
        return self._waiter.cancel_all(reason)

    # ------------------------------------------------------------------
    # User-input interaction (ask_user and friends)
    # ------------------------------------------------------------------

    async def request_user_input(
        self,
        question: str,
        *,
        options: list[dict[str, str]] | None = None,
        source: str = "interaction",
        timeout_seconds: float | None = None,
        tool_call_id: str = "",
    ) -> dict[str, Any]:
        """Publish and resolve one user-input request owned by this plugin.

        The caller owns ``CLIENT_EVENT`` dispatch (the tool pipeline emits
        it alongside the tool message).  This service only routes the event
        through the installed live sink (preferred) or the fallback waiter.
        Without a live sink the request fails closed to ``unsupported`` so
        the turn never hangs on a client that cannot answer.  Returns the
        answered dict (``status=answered`` with the answer, or a closed
        status such as ``disconnected`` / ``timeout`` / ``cancelled`` /
        ``unsupported``).
        """
        request_id = f"user_input:{tool_call_id or uuid.uuid4().hex}"
        client_event = {
            "type": "user_input_required",
            "data": {
                "request_id": request_id,
                "tool_call_id": tool_call_id,
                "source": source,
                "question": question,
                "options": list(options or []),
                "timeout_seconds": timeout_seconds,
            },
        }
        await self.ctx.emit(
            Events.CLIENT_EVENT,
            EventContext(client_event=client_event),
        )
        sink_result = await self.client_events.request(
            client_event,
            timeout_seconds=timeout_seconds,
            tool_call_id=tool_call_id,
        )
        if sink_result is not None:
            return sink_result
        wait_timeout = 0 if timeout_seconds is None else timeout_seconds
        result = await self._waiter.wait(request_id, wait_timeout)
        if result.status == "timeout" and timeout_seconds is None:
            return {
                "request_id": result.request_id,
                "status": "unsupported",
                "reason": "live_user_input_unsupported",
            }
        return {
            "answer": getattr(result, "answer", ""),
            "request_id": result.request_id,
            "status": result.status,
            "reason": result.reason,
        }


class InteractionsComponent:
    """Register the interactions service as ``ctx.interactions``."""

    inject = ["tools", "client_events"]
    name = "xbot.interactions"

    def apply(self, ctx: Any, config: Any = None) -> None:
        config = config or {}
        service = InteractionsService(ctx, ctx.client_events)
        ctx.set("interactions", service)
        ctx.dispose(ctx.client_events.register_waiter(
            "user_input_required", service.waiter
        ))
        from XBotv2.interactions.tools import ask_user, send_message

        ctx.tools.register(send_message)
        if bool(config.get("interactive", True)):
            ctx.tools.register(ask_user, injected={"interactions": service})
        ctx.on(
            Events.SESSION_CLOSE,
            lambda _event: service.cancel_all("session_closed"),
        )


plugin = InteractionsComponent()
