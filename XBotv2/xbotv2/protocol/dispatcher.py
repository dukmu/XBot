"""Per-session engine dispatcher for the HTTP/SSE transport.

The HTTP server in ``xbotv2.protocol.http_server`` keeps a single
``SessionManager`` that owns one ``SessionContext`` per
``(session_id, thread_id)``. Each context holds:

- an ``Engine`` instance (created via ``bootstrap``),
- a per-session ``asyncio.Lock`` so only one turn runs at a time,
- a per-turn ``asyncio.Queue`` used to bridge engine events and the
  sink-driven live interaction events into a single ordered stream for
  the SSE response,
- a per-turn ``asyncio.Event`` for "client disconnected" propagation
  so the engine stops blocking on a live interaction when the HTTP
  request is aborted by the client.

This module is transport-agnostic: nothing here imports ``fastapi`` or
``aiohttp``. The HTTP layer wires its request lifecycle to the
``run_turn_stream`` async generator and the ``submit_*`` methods on the
engine.

See ``docsv2/tui_opencode_requirements.md`` §10.5.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator

from xbotv2.core.bootstrap import bootstrap

logger = logging.getLogger("xbotv2.dispatcher")


class SessionNotFound(KeyError):
    """The caller asked for a session that has not been opened."""


class SessionBusy(RuntimeError):
    """The session is already processing a turn; the new request is rejected."""


@dataclass
class TurnEventBus:
    """Bridge between the engine coroutine and the SSE response.

    The engine-pump task pushes one dict per turn event into ``events``.
    The sink that handles live interactions also pushes events here so
    the consumer sees a single ordered stream. ``disconnected`` is set
    when the HTTP request is aborted by the client, so the sink can
    stop waiting on the engine's interaction waiter.
    """

    events: asyncio.Queue[dict[str, Any] | None] = field(
        default_factory=asyncio.Queue
    )
    disconnected: asyncio.Event = field(default_factory=asyncio.Event)


@dataclass
class SessionContext:
    """One engine + per-turn coordination state."""

    session_id: str
    thread_id: str
    provider_name: str
    data_dir: str
    workspace_root: str
    no_plugins: bool
    engine: Any
    turn_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    last_bus: TurnEventBus | None = None
    # The asyncio.Task running the current turn's engine iterator. Set
    # when a turn starts; cleared when it finishes. The HTTP server's
    # ``/interrupt`` endpoint cancels this task to abort the turn.
    turn_task: asyncio.Task | None = None
    # When set, the in-flight SSE stream will be closed at the next
    # event boundary. The dispatcher flips this on interrupt and the
    # generator exits cleanly.
    interrupt_requested: bool = False

    def request_interrupt(self) -> bool:
        """Cancel the running turn task. Returns True if there was one.

        Snaps the task reference locally to avoid a TOCTOU race with
        ``run_turn_stream``'s finally block (which sets
        ``self.turn_task = None``).  Calling ``cancel()`` on a done
        task is always safe in Python 3.9+, so the worst case is a
        no-op — we never crash with ``AttributeError``.
        """

        task = self.turn_task
        if task is None or task.done():
            return False
        self.interrupt_requested = True
        task.cancel()
        return True

    async def close(self) -> None:
        try:
            await self.engine.close_session()
        except Exception:
            logger.exception("Engine close_session failed for %s", self.session_id)


class SessionManager:
    """Owns every active SessionContext keyed by ``session_id``."""

    def __init__(self) -> None:
        self._sessions: dict[str, SessionContext] = {}
        self._lock = asyncio.Lock()

    @property
    def size(self) -> int:
        return len(self._sessions)

    async def get(self, session_id: str) -> SessionContext:
        async with self._lock:
            ctx = self._sessions.get(session_id)
        if ctx is None:
            raise SessionNotFound(session_id)
        return ctx

    async def open_session(
        self,
        *,
        session_id: str | None,
        thread_id: str,
        provider_name: str,
        data_dir: str,
        workspace_root: str,
        mode: str = "new",
        no_plugins: bool,
        llm_override: Any | None = None,
    ) -> SessionContext:
        async with self._lock:
            mode = (mode or "new").lower().strip()
            if mode not in {"new", "resume"}:
                raise ValueError("session mode must be new or resume")
            if mode == "resume" and not session_id:
                raise ValueError("resume mode requires session_id")
            if mode == "new":
                session_id = session_id or _new_session_id()
                if session_id in self._sessions:
                    raise ValueError(f"session already exists: {session_id}")
            assert session_id is not None
            existing = self._sessions.get(session_id)
            if existing is not None:
                if mode == "resume":
                    return existing
                raise ValueError(f"session already exists: {session_id}")
            state_root = Path(data_dir) / "sessions" / session_id / "state"
            if mode == "resume" and not state_root.exists():
                raise SessionNotFound(session_id)
            if mode == "new" and state_root.exists():
                raise ValueError(f"session state already exists: {session_id}")
            engine = await bootstrap(
                config_dir=data_dir,
                provider_name=provider_name,
                session_id=session_id,
                thread_id=thread_id,
                workspace_root=workspace_root,
                plugin_dirs=[] if no_plugins else None,
                llm_override=llm_override,
            )
            await engine.start_session()
            ctx = SessionContext(
                session_id=session_id,
                thread_id=thread_id,
                provider_name=provider_name,
                data_dir=data_dir,
                workspace_root=workspace_root,
                no_plugins=no_plugins,
                engine=engine,
            )
            self._sessions[session_id] = ctx
            return ctx

    async def close_session(self, session_id: str) -> None:
        async with self._lock:
            ctx = self._sessions.pop(session_id, None)
        if ctx is not None:
            await ctx.close()

    async def close_all(self) -> None:
        async with self._lock:
            contexts = list(self._sessions.values())
            self._sessions.clear()
        for ctx in contexts:
            await ctx.close()


def _new_session_id() -> str:
    from datetime import datetime

    return f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:4]}"


def _event_to_payload(event: dict[str, Any]) -> dict[str, Any]:
    """Normalize a runtime event dict to ``{type, data}`` for SSE consumers."""

    return {
        "type": event.get("type", ""),
        "data": event.get("data", {}),
    }


@asynccontextmanager
async def live_interaction_sink(
    ctx: SessionContext,
    bus: TurnEventBus,
) -> AsyncIterator[None]:
    """Install a client_event_sink that bridges live interactions to ``bus``.

    While the context is active, every ``permission_request`` or
    ``user_input_required`` raised by the engine is enqueued as an
    SSE event on ``bus.events``, then the sink awaits the engine's
    internal ``InteractionWaiter`` for the same request id. The
    waiter is resolved by the HTTP interaction endpoints.

    The context also arms ``bus.disconnected``: when the HTTP request
    is cancelled, the sink cancels the pending waiter so the engine
    continues with a "disconnected" result instead of hanging.
    """

    disconnect_task = asyncio.create_task(bus.disconnected.wait())

    async def sink(
        client_event: dict[str, Any],
        *,
        timeout_seconds: float | None = None,
        tool_call_id: str = "",
    ) -> dict[str, Any]:
        del tool_call_id
        event_type = str(client_event.get("type") or "")
        event_data = client_event.get("data") or {}
        req_id = str(event_data.get("request_id") or "")

        # Push the request event into the SSE stream so the client sees it.
        await bus.events.put(_event_to_payload(client_event))

        if event_type == "permission_request":
            waiter = ctx.engine._permission_waiter  # noqa: SLF001
        else:
            waiter = ctx.engine._user_input_waiter  # noqa: SLF001

        wait_task = asyncio.create_task(waiter.wait(req_id, timeout_seconds))
        try:
            done, _pending = await asyncio.wait(
                {wait_task, disconnect_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
        except BaseException:
            wait_task.cancel()
            raise
        if wait_task in done:
            try:
                result = wait_task.result()
            except Exception as exc:  # noqa: BLE001 — surface to sink
                return {
                    "request_id": req_id,
                    "status": "error",
                    "reason": str(exc),
                }
            state = (
                ctx.engine.record_permission_result(client_event, result.__dict__)
                if event_type == "permission_request"
                else ctx.engine.record_user_input_result(client_event, result.__dict__)
            )
            ack_type = (
                "permission_response_recorded"
                if event_type == "permission_request"
                else "user_input_recorded"
            )
            ack_payload = {
                "request_id": req_id,
                "status": result.status,
                "decision": result.decision,
                "scope": result.scope,
                "answer": result.answer,
                "pending_interactions": state.get("pending_interactions", []),
            }
            await bus.events.put({"type": ack_type, "data": ack_payload})
            return result.__dict__
        # Disconnect won: signal a "disconnected" result so the engine proceeds.
        wait_task.cancel()
        try:
            await wait_task
        except (asyncio.CancelledError, Exception):
            pass
        return {
            "request_id": req_id,
            "status": "disconnected",
            "reason": "client_disconnected",
        }

    previous = ctx.engine.set_client_event_sink(sink)
    try:
        yield
    finally:
        ctx.engine.set_client_event_sink(previous)
        if not disconnect_task.done():
            disconnect_task.cancel()
            try:
                await disconnect_task
            except (asyncio.CancelledError, Exception):
                pass


async def _drain_engine_into_bus(
    ctx: SessionContext,
    bus: TurnEventBus,
    content: str,
) -> None:
    """Pump every event from ``engine.run_turn`` into ``bus.events``."""

    try:
        async for event in ctx.engine.run_turn(content):
            await bus.events.put(_event_to_payload(event))
    except asyncio.CancelledError:
        # TUI pressed ESC. The engine's ``run_turn`` already yielded a
        # structured ``turn_cancelled`` event (which the pump forwarded
        # to the bus) before re-raising.  We only need to log and
        # re-raise so the finally block pushes the ``None`` sentinel
        # and the caller in ``run_turn_stream`` can swallow it cleanly.
        logger.info("Turn cancelled for session %s", ctx.session_id)
        raise
    except Exception as exc:  # noqa: BLE001 — surface as one error event
        logger.exception("Engine run_turn failed")
        await bus.events.put({
            "type": "error",
            "data": {"code": "turn_failed", "message": str(exc)},
        })
    finally:
        await bus.events.put(None)


async def run_turn_stream(
    ctx: SessionContext,
    *,
    content: str,
) -> AsyncIterator[dict[str, Any]]:
    """Run one user turn and yield a single ordered stream of events.

    Yields:
      dicts with ``type`` and ``data`` keys, in the order they are
      produced by the engine and the live interaction sink. The stream
      ends after the engine iterator completes; the caller should also
      expect a trailing ``None`` sentinel pushed by the pump.
    """

    if ctx.turn_lock.locked():
        raise SessionBusy(ctx.session_id)

    async with ctx.turn_lock:
        bus = TurnEventBus()
        ctx.last_bus = bus
        ctx.interrupt_requested = False
        pump_task = asyncio.create_task(_drain_engine_into_bus(ctx, bus, content))
        ctx.turn_task = pump_task
        try:
            async with live_interaction_sink(ctx, bus):
                while True:
                    event = await bus.events.get()
                    if event is None:
                        break
                    yield event
        finally:
            ctx.last_bus = None
            ctx.turn_task = None
            # Drain the pump so it can't outlive the consumer.
            # Swallow ``CancelledError`` here: the pump's except
            # branch already pushed a ``turn_cancelled`` event to
            # the bus (which the consumer read), so the consumer
            # has all the information it needs. Re-raising would
            # tear the ASGI response body mid-flight ("ASGI
            # callable returned without completing response") and
            # force the TUI to handle a half-closed stream.
            try:
                await pump_task
            except asyncio.CancelledError:
                pass
            except Exception:  # noqa: BLE001
                logger.exception(
                    "pump_task ended with error for session %s", ctx.session_id
                )
