"""The transport: one place that talks to the server and reports what happened.

It owns four things the rest of the client must not worry about:

* **sequence continuity** -- frames are applied once, in order. A gap is reported
  to the user and the frame is still applied; a replayed frame (which recovery
  produces on purpose) is not applied twice.
* **recovery** -- an evicted cursor resubscribes from the oldest retained frame,
  and a rebuild re-opens the session for a fresh baseline. Both are bounded, and
  running out of budget is reported instead of looping.
* **the watchdog** -- the thread listing is the server's own answer for whether a
  turn is running. The session descriptor has no such field, so this read is what
  keeps the status honest after a lost terminal frame or a mid-turn attach.
* **writes** -- submission and interrupt. A submission carries a client-generated
  id, so the client can recognise its own message instead of matching on content.

Everything it produces is a ``UiEvent``; it never touches the reducer or the DOM.
The backend is a structural port, so the real ``XBotClient`` is used unchanged and
tests can script the server without re-implementing the client.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from collections.abc import Awaitable
from typing import AsyncGenerator, Callable, Literal, Protocol, Sequence

from XBotv2.agents import AgentListResponse, AgentSelectionResponse
from XBotv2.agentloop.protocol import LoopError
from XBotv2.commands import (
    CommandDescription,
    CommandExecution,
    CommandListResponse,
    CommandResponse,
)
from XBotv2.client import XBotClientError
from XBotv2.core.domain import Cursor
from XBotv2.core.history import HistoryPage
from XBotv2.interactions.protocol import InteractionResponse
from XBotv2.llm import EffortSelectionResponse, ProviderCatalog, ProviderSelectionResponse
from XBotv2.protocol import ServerEvent
from XBotv2.protocol.models import HelloResponse
from XBotv2.session import (
    AttachmentInput,
    ImageInput,
    OpenSessionResponse,
    SessionListResponse,
    SessionMode,
    SessionSummary,
    ThreadListResponse,
    ThreadSummary,
)
from XBotv2.session.protocol import InterruptResponse
from XBotv2.session.records import ConversationRecord
from XBotv2.tui.events import (
    ConnectionChanged,
    ErrorFrame,
    InterruptAsked,
    InterruptSettled,
    OlderHistoryFailed,
    OlderHistoryLoaded,
    SnapshotAdopted,
    StatusSlotsUpdated,
    StreamGapDetected,
    UiEvent,
    UserInputFailed,
    UserInputSubmitted,
    ThreadRead,
)
from XBotv2.tui.protocol import (
    FrameRejected,
    FrameTranslator,
    replay_pending_interactions,
)
from XBotv2.tui.status import Connection

CURSOR_EXPIRED = "session_event_cursor_expired"

#: The default window one attach asks for, and the default number of entries the
#: client keeps before releasing the pages the reader walked past.
DEFAULT_HISTORY_WINDOW = 50
DEFAULT_HISTORY_RETENTION = 2000


class SessionBackend(Protocol):
    """The slice of the HTTP client the transport uses.

    ``XBotClient`` satisfies this structurally, so production passes it directly
    and a test can script the server without re-implementing the client.
    """

    async def hello(
        self,
        *,
        client_name: str,
        session_id: str | None,
        thread_id: str,
    ) -> HelloResponse: ...

    async def open_session(
        self,
        *,
        session_id: str | None,
        thread_id: str,
        workspace_root: str | None,
        mode: SessionMode,
        agent: str | None,
        history_limit: int | None,
    ) -> OpenSessionResponse: ...

    async def list_sessions(self) -> SessionListResponse: ...

    async def list_threads(self, session_id: str) -> ThreadListResponse: ...

    async def list_commands(
        self, session_id: str, thread_id: str
    ) -> CommandListResponse: ...

    async def list_messages(
        self,
        session_id: str,
        thread_id: str,
        *,
        cursor: Cursor | None = None,
        limit: int | None = None,
    ) -> HistoryPage[ConversationRecord]: ...

    async def list_providers(self) -> ProviderCatalog: ...

    async def list_agents(self, session_id: str, thread_id: str) -> AgentListResponse: ...

    async def select_provider(
        self, session_id: str, thread_id: str, name: str, model: str | None = None
    ) -> ProviderSelectionResponse: ...

    async def select_effort(
        self, session_id: str, thread_id: str, effort: str
    ) -> EffortSelectionResponse: ...

    async def select_agent(
        self, session_id: str, thread_id: str, name: str
    ) -> AgentSelectionResponse: ...

    async def run_command(
        self, session_id: str, thread_id: str, *, raw: str
    ) -> CommandResponse: ...

    async def send_message(
        self,
        session_id: str,
        thread_id: str,
        content: str,
        *,
        request_id: str,
        delivery: Literal["queue", "steer"],
        images: list[ImageInput],
        attachments: list[AttachmentInput],
    ) -> None: ...

    async def interrupt(self, session_id: str, thread_id: str) -> InterruptResponse: ...

    async def respond_permission(
        self,
        session_id: str,
        thread_id: str,
        *,
        request_id: str,
        decision: Literal["allow", "deny"],
        scope: Literal["once", "session"] = "once",
    ) -> InteractionResponse: ...

    async def respond_user_input(
        self,
        session_id: str,
        thread_id: str,
        *,
        request_id: str,
        answer: str,
    ) -> InteractionResponse: ...

    def stream_events(
        self, session_id: str, thread_id: str, *, after: int | None = None
    ) -> AsyncGenerator[ServerEvent, None]: ...


@dataclass(frozen=True)
class TransportConfig:
    """Everything the transport needs to attach to one thread."""

    session_id: str = ""
    thread_id: str = "agent"
    workspace_root: str | None = None
    mode: SessionMode = "new"
    agent: str | None = None
    #: The client's window: how many of the newest entries one attach asks for,
    #: and how many each page further back carries. It is the paging granularity,
    #: not a retention bound -- the reader can always walk further back.
    history_window: int = DEFAULT_HISTORY_WINDOW
    #: How many entries the client holds before it lets the pages the reader
    #: walked past go again. Only residency: the cursor chain restores them, so
    #: nothing becomes unreachable.
    history_retention: int = DEFAULT_HISTORY_RETENTION
    watchdog_seconds: float = 5.0
    reconnect_delays: tuple[float, ...] = (0.05, 0.1, 0.25)
    cursor_recoveries: int = 3
    baseline_rebuilds: int = 2


class TransportSession:
    """One attached thread: connect, read, recover, write."""

    def __init__(
        self,
        backend: SessionBackend,
        *,
        config: TransportConfig | None = None,
        emit: Callable[[UiEvent], None],
        sleep: Callable[[float], Awaitable[None]] | None = None,
        new_input_id: Callable[[], str] | None = None,
    ) -> None:
        self._backend = backend
        self._config = config or TransportConfig()
        self._emit = emit
        self._sleep = sleep or asyncio.sleep
        self._new_input_id = new_input_id or (lambda: f"input-{uuid.uuid4().hex}")
        self._session_id = self._config.session_id
        self._thread_id = self._config.thread_id
        self._translator = FrameTranslator(
            session_id=self._session_id, thread_id=self._thread_id
        )
        self._cursor = 0
        self._stopped = False
        self._watchdog_failures = 0
        self._cursor_recoveries = 0
        self._baseline_rebuilds = 0
        # Switching sessions moves the read loop onto another subscription. The
        # old one is idle, so it cannot be waited on for a frame to notice.
        self._restart = asyncio.Event()
        self._restarting = False

    @property
    def cursor(self) -> int:
        """The last applied sequence; the point a resubscription resumes from."""
        return self._cursor

    @cursor.setter
    def cursor(self, value: int) -> None:
        self._cursor = max(0, int(value))

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def thread_id(self) -> str:
        return self._thread_id

    def stop(self) -> None:
        self._stopped = True
        # The read loop may be blocked waiting for a frame on an idle
        # subscription; stopping must not wait for the server to say something.
        self._restart.set()

    # --- connect ------------------------------------------------------

    async def connect(self) -> OpenSessionResponse:
        """Handshake and adopt the session baseline.

        Raises whatever the server raised, after reporting it: the caller decides
        whether to retry, and the user sees that the connection failed.
        """
        self._emit(ConnectionChanged(Connection.CONNECTING))
        try:
            await self._backend.hello(
                client_name="xbotv2-tui",
                session_id=self._config.session_id or None,
                thread_id=self._config.thread_id,
            )
            self._translator = FrameTranslator(
                session_id=self._session_id, thread_id=self._thread_id
            )
            opened = await self._open(
                mode=self._config.mode,
                session_id=self._session_id or None,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._emit(ErrorFrame(payload=LoopError(code="connect_failed", message=str(exc))))
            self._emit(ConnectionChanged(Connection.DISCONNECTED, str(exc)))
            raise
        self._attached_to(opened)
        self._adopt(opened)
        # The descriptor carries no turn answer, so the thread read is what
        # settles whether a turn is already running on attach.
        await self.watchdog_once()
        self._emit(ConnectionChanged(Connection.CONNECTED))
        return opened

    def _attached_to(
        self,
        opened: OpenSessionResponse,
    ) -> None:
        """Adopt the identity owned by the opened thread resource."""
        self._session_id = opened.data.key.session_id
        self._thread_id = opened.data.key.thread_id
        self._translator = FrameTranslator(
            session_id=self._session_id, thread_id=self._thread_id
        )

    def _adopt(self, opened: OpenSessionResponse) -> None:
        self._emit(SnapshotAdopted(opened))
        for event in replay_pending_interactions(opened.data.pending_interactions):
            self._emit(event)
        self._cursor = opened.data.event_cursor

    async def switch(
        self,
        *,
        session_id: str,
        thread_id: str,
        mode: SessionMode = "resume",
    ) -> OpenSessionResponse:
        """Attach to another session without tearing the client down.

        A failed switch changes nothing: the client stays where it was, and the
        caller decides what to tell the user.
        """
        if not thread_id:
            thread_id = await self._main_thread(session_id)
        opened = await self._open(
            mode=mode, session_id=session_id or None, thread_id=thread_id,
        )
        self._attached_to(opened)
        self._cursor_recoveries = 0
        self._baseline_rebuilds = 0
        self._adopt(opened)
        self._restart.set()
        await self.watchdog_once()
        return opened

    async def _main_thread(self, session_id: str) -> str:
        """The thread a session is attached by: its main one, or its first."""
        listing = await self._backend.list_threads(session_id)
        threads = listing.threads
        main = next(
            (item for item in threads if item.kind == "main"),
            None,
        )
        chosen = main if main is not None else (threads[0] if threads else None)
        if chosen is None:
            raise ValueError(f"session {session_id!r} has no thread to attach to")
        return chosen.thread_id

    async def list_providers(self) -> ProviderCatalog:
        """The provider/model catalogue, for the pickers."""
        return await self._backend.list_providers()

    async def list_agents(self) -> AgentListResponse:
        """The agents this thread can switch to."""
        return await self._backend.list_agents(self._session_id, self._thread_id)

    async def select_provider(
        self, name: str, model: str | None = None
    ) -> ProviderSelectionResponse:
        return await self._backend.select_provider(
            self._session_id, self._thread_id, name, model
        )

    async def select_effort(self, effort: str) -> EffortSelectionResponse:
        return await self._backend.select_effort(self._session_id, self._thread_id, effort)

    async def select_agent(self, name: str) -> AgentSelectionResponse:
        return await self._backend.select_agent(self._session_id, self._thread_id, name)

    async def list_commands(self) -> Sequence[CommandDescription]:
        """The commands the server offers for the attached thread."""
        response = await self._backend.list_commands(self._session_id, self._thread_id)
        return tuple(response.commands)

    async def run_command(self, raw: str) -> CommandExecution:
        """Run one server-owned command and return what it answered.

        The line goes over as typed: the server owns the catalogue, so it is what
        splits the name from its arguments and decides whether the command can
        run at all.
        """
        response = await self._backend.run_command(self._session_id, self._thread_id, raw=raw)
        return response.data

    async def respond_permission(
        self,
        request_id: str,
        decision: Literal["allow", "deny"],
        scope: Literal["once", "session"] = "once",
    ) -> None:
        await self._backend.respond_permission(
            self._session_id,
            self._thread_id,
            request_id=request_id,
            decision=decision,
            scope=scope,
        )

    async def respond_user_input(self, request_id: str, answer: str) -> None:
        await self._backend.respond_user_input(
            self._session_id,
            self._thread_id,
            request_id=request_id,
            answer=answer,
        )

    async def list_threads(self) -> tuple[ThreadSummary, ...]:
        """The threads this session holds, for the thread picker."""
        listing = await self._backend.list_threads(self._session_id)
        return tuple(listing.threads)

    async def list_sessions(self) -> tuple[SessionSummary, ...]:
        """The sessions this server holds, for the session picker."""
        listing = await self._backend.list_sessions()
        return tuple(listing.sessions)

    # --- reading ------------------------------------------------------

    async def run(self) -> None:
        """Read the event stream until stopped or out of recovery budget."""
        attempt = 0
        while not self._stopped:
            try:
                applied = await self._consume_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if self._stopped:
                    return
                if self._rewind_cursor(exc):
                    continue
                if await self._rebuild_baseline(exc):
                    continue
                if attempt >= len(self._config.reconnect_delays):
                    self._fail(exc)
                    return
                await self._sleep(self._config.reconnect_delays[attempt])
                attempt += 1
                continue
            if self._stopped:
                return
            if self._restarting:
                # A deliberate move to another session, not a broken stream.
                self._restarting = False
                attempt = 0
                continue
            if applied:
                attempt = 0
            if attempt >= len(self._config.reconnect_delays):
                self._fail(
                    RuntimeError("session event stream ended without a shutdown")
                )
                return
            await self._sleep(self._config.reconnect_delays[attempt])
            attempt += 1

    async def _consume_once(self) -> int:
        """Read one subscription until it ends, fails, or is restarted.

        The frame read races the restart signal, because a switch must take
        effect while the current subscription is simply idle.
        """
        applied = 0
        stream = self._backend.stream_events(
            self._session_id, self._thread_id, after=self._cursor
        )
        iterator = stream
        read: asyncio.Task | None = None
        restart: asyncio.Task | None = None
        try:
            while not self._stopped:
                read = asyncio.create_task(iterator.__anext__())
                restart = asyncio.create_task(self._restart.wait())
                done, pending = await asyncio.wait(
                    {read, restart}, return_when=asyncio.FIRST_COMPLETED
                )
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
                if restart in done:
                    self._restart.clear()
                    self._restarting = True
                    return applied
                try:
                    frame = read.result()
                except StopAsyncIteration:
                    return applied
                if self._apply(frame):
                    applied += 1
            return applied
        finally:
            child_tasks = [
                task for task in (read, restart) if task is not None
            ]
            for task in child_tasks:
                if not task.done():
                    task.cancel()
            if child_tasks:
                await asyncio.gather(*child_tasks, return_exceptions=True)
            await iterator.aclose()

    def _apply(self, frame: ServerEvent) -> bool:
        sequence = frame.sequence
        if sequence and sequence <= self._cursor:
            # Recovery resubscribes behind the cursor on purpose, so the server
            # replays frames already applied. Applying them again would double
            # every counter they carry.
            return False
        if sequence and self._cursor and sequence > self._cursor + 1:
            self._emit(
                StreamGapDetected(expected=self._cursor + 1, received=sequence)
            )
        try:
            events = self._translator.translate(frame)
        except FrameRejected as exc:
            # A frame this client cannot apply is a visible failure, never a
            # skip: the cursor still advances, because the frame was consumed.
            self._emit(ErrorFrame(payload=LoopError(code="rejected_frame", message=str(exc))))
            events = ()
        for event in events:
            self._emit(event)
        if sequence:
            self._cursor = sequence
        return True

    def _rewind_cursor(self, exc: BaseException) -> bool:
        """Resubscribe from the oldest frame the server still retains."""
        if not isinstance(exc, XBotClientError) or exc.code != CURSOR_EXPIRED:
            return False
        if self._cursor_recoveries >= self._config.cursor_recoveries:
            return False
        oldest = exc.details.get("oldest_sequence")
        if not isinstance(oldest, int) or isinstance(oldest, bool) or oldest < 1:
            return False
        self._cursor = max(0, oldest - 1)
        self._cursor_recoveries += 1
        return True

    async def _rebuild_baseline(self, exc: BaseException) -> bool:
        """Re-open the session for a fresh cursor and history."""
        if not isinstance(exc, XBotClientError) or exc.code != CURSOR_EXPIRED:
            return False
        if self._baseline_rebuilds >= self._config.baseline_rebuilds:
            return False
        opened = await self._open(mode="resume", session_id=self._session_id or None)
        self._baseline_rebuilds += 1
        self._cursor_recoveries = 0
        self._adopt(opened)
        return True

    async def _open(
        self,
        *,
        mode: SessionMode,
        session_id: str | None,
        thread_id: str | None = None,
    ) -> OpenSessionResponse:
        """Attach to one thread, window included.

        This is the only attach request the client sends. The first attach,
        recovery, and a thread switch must all ask for the same window, and
        spelling the request out per call is how one of them ends up asking for
        the whole conversation.
        """
        return await self._backend.open_session(
            session_id=session_id,
            thread_id=thread_id or self._thread_id,
            workspace_root=self._config.workspace_root,
            mode=mode,
            agent=self._config.agent or None,
            history_limit=self._config.history_window,
        )

    async def load_older(self, cursor: Cursor) -> None:
        """Read the page before ``cursor`` and hand it to the reducer.

        A page that cannot be read is a visible, retryable state rather than an
        exception: the reader asked for history, and the client must tell them it
        could not fetch it instead of swallowing the request.
        """
        try:
            page = await self._backend.list_messages(
                self._session_id,
                self._thread_id,
                cursor=cursor,
                limit=self._config.history_window,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._emit(OlderHistoryFailed(message=str(exc) or exc.__class__.__name__))
            return
        self._emit(OlderHistoryLoaded(payload=page, cursor=cursor))

    def _fail(self, exc: BaseException) -> None:
        self._emit(ErrorFrame(payload=LoopError(code="stream_failed", message=str(exc))))
        self._emit(ConnectionChanged(Connection.DISCONNECTED, str(exc)))

    # --- watchdog -----------------------------------------------------

    async def watchdog_once(self) -> bool:
        """One authoritative read of the attached thread.

        Reports a failure only when it is the first of a run, so an unreachable
        server is visible without one error line per tick.
        """
        try:
            listing = await self._backend.list_threads(self._session_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._note_watchdog_failure(str(exc))
            return False
        summary = _find_thread(listing.threads, self._thread_id)
        if summary is None:
            self._note_watchdog_failure(
                f"thread {self._thread_id!r} is no longer listed for session "
                f"{self._session_id!r}"
            )
            return False
        self._watchdog_failures = 0
        self._emit(ThreadRead(payload=summary))
        if summary.status_slots:
            self._emit(StatusSlotsUpdated(dict(summary.status_slots)))
        return True

    async def watch(self) -> None:
        """Poll the thread on the configured interval while the client runs."""
        while not self._stopped:
            await self._sleep(self._config.watchdog_seconds)
            if self._stopped:
                return
            await self.watchdog_once()

    def _note_watchdog_failure(self, message: str) -> None:
        if self._watchdog_failures == 0:
            self._emit(ErrorFrame(payload=LoopError(code="watchdog_failed", message=message)))
        self._watchdog_failures += 1

    # --- writes -------------------------------------------------------

    async def submit(
        self,
        text: str,
        *,
        delivery: str = "steer",
        images: Sequence[ImageInput] = (),
    ) -> str:
        """Send input, binding it to a client-generated id.

        The id is what the server echoes back, so the client recognises its own
        message by identity rather than by matching content.
        """
        input_id = self._new_input_id()
        self._emit(UserInputSubmitted(input_id=input_id, content=text))
        try:
            await self._backend.send_message(
                self._session_id,
                self._thread_id,
                text,
                request_id=input_id,
                delivery=delivery,
                images=list(images),
                attachments=[],
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._emit(UserInputFailed(input_id=input_id, error=str(exc)))
        return input_id

    async def interrupt(self) -> None:
        """Ask the server to stop the running turn."""
        self._emit(InterruptAsked())
        try:
            result = await self._backend.interrupt(self._session_id, self._thread_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._emit(ErrorFrame(payload=LoopError(code="interrupt_failed", message=str(exc))))
            self._emit(InterruptSettled(cancelled=False))
            return
        self._emit(InterruptSettled(cancelled=result.cancelled))

def _find_thread(
    threads: Sequence[ThreadSummary], thread_id: str
) -> ThreadSummary | None:
    for summary in threads:
        if summary.thread_id == thread_id:
            return summary
    return None


__all__ = ["SessionBackend", "TransportConfig", "TransportSession"]
