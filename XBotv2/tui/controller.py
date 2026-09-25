"""The controller: the one place that wires transport, reducer, and views.

It owns the session state, feeds every incoming event through the reducer, and
tells the views to redraw. Two properties it is responsible for:

* **rendering is coalesced** -- dispatching an event never renders, and a burst of
  events costs one render, so render work cannot pile up behind the stream;
* **the views are told, never asked** -- the controller builds the models
  (status, jobs, composer) from the state, so no view re-derives anything.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Awaitable, Callable, Literal, Protocol, Sequence

from XBotv2.jobs.contracts import JobView
from XBotv2.interactions.protocol import UserInputRequest
from XBotv2.permissions.contracts import PermissionRequest
from XBotv2.session.contracts import PendingInputData
from XBotv2.session.contracts import ImageInput
from XBotv2.tui.events import OlderHistoryRequested, TranscriptCleared, UiEvent
from XBotv2.tui.state import (
    HistoryKnown,
    SessionState,
    reduce,
    release_oldest_loaded_page,
)
from XBotv2.tui.status import ServerTurn
from XBotv2.tui.transport import TransportConfig, TransportSession
from XBotv2.tui.view.composer import ComposerModel, composer_delivery
from XBotv2.tui.view.status_bar import StatusLine, status_line_for


class ViewPort(Protocol):
    """What the controller needs from the UI.

    The Textual adapter implements this; a test can record calls instead. It is a
    port, not a second client: it renders what it is given and owns only its own
    scroll position.
    """

    async def render_transcript(self, state: SessionState) -> bool: ...

    @property
    def reader_at_end(self) -> bool: ...

    def render_status(self, model: StatusLine) -> None: ...

    def render_jobs(self, jobs: Sequence[JobView]) -> None: ...

    def render_queue(self, items: Sequence[PendingInputData]) -> None: ...

    def render_composer(self, model: ComposerModel) -> None: ...

    async def page_older(self, state: SessionState) -> bool: ...

    async def page_newer(self, state: SessionState) -> bool: ...

    async def go_to_tail(self, state: SessionState) -> None: ...


class TuiController:
    """The session UI's brain, independent of Textual."""

    def __init__(
        self,
        backend: Any,
        *,
        view: ViewPort,
        config: TransportConfig | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        new_input_id: Callable[[], str] | None = None,
        workspace: str = "",
    ) -> None:
        config = config or TransportConfig()
        self._config = config
        self.state = SessionState()
        self._view = view
        self._clock = clock
        self._sleep = sleep
        self._workspace = workspace
        self._dirty = True
        self._stopped = False
        # Images waiting for the next message. Composing the next input is the
        # client's business, so they live here rather than in session state.
        self._attachments: list[ImageInput] = []
        self.transport = TransportSession(
            backend,
            config=config,
            emit=self.dispatch,
            sleep=sleep,
            new_input_id=new_input_id,
        )

    # --- state --------------------------------------------------------

    @property
    def dirty(self) -> bool:
        return self._dirty

    def dispatch(self, event: UiEvent) -> None:
        """Apply one event and mark the views stale. Never renders."""
        reduce(self.state, event, now=self._clock())
        self._dirty = True

    def stop(self) -> None:
        self._stopped = True
        self.transport.stop()

    # --- rendering ----------------------------------------------------

    async def flush(self) -> bool:
        """Redraw the views if anything changed since the last flush.

        Retention is evaluated even when no event arrived: the reader returning
        to the tail is a change only the view can see, and the pages they walked
        past must not stay held until the next event happens to arrive.
        """
        released = self._release_held_pages()
        if not self._dirty and not released:
            return False
        self._dirty = False
        await self._view.render_transcript(self.state)
        self._view.render_status(self.status_model())
        self._view.render_jobs(tuple(self.state.jobs.values()))
        self._view.render_queue(self.state.queue)
        self._view.render_composer(self.composer_model())
        return True

    def _release_held_pages(self) -> bool:
        """Bound residency by letting the reader's own oldest pages go.

        Only while the reader is following the tail: a page they are looking at
        must not be released under them. Nothing becomes unreachable -- each
        release puts back the cursor that preceded the page, so it can be loaded
        again -- and the window the attach returned is never released, so a
        client that never paged back never shrinks.
        """
        released = False
        while (
            len(self.state.timeline) > self._config.history_retention
            and self.state.loaded_pages
        ):
            if not self._view.reader_at_end:
                return released
            release_oldest_loaded_page(self.state)
            released = True
        return released

    async def flush_loop(self, interval: float = 0.1) -> None:
        """Redraw on a fixed cadence while events keep arriving.

        A single loop owns the frame: the transport can dispatch as fast as the
        server sends without each event costing a render.
        """
        while not self._stopped:
            await self._sleep(interval)
            if self._stopped:
                return
            await self.flush()

    # --- models -------------------------------------------------------

    def status_model(self) -> StatusLine:
        return status_line_for(
            self.state,
            workspace=self._workspace,
            activity=self.activity(),
        )

    def composer_model(self) -> ComposerModel:
        return ComposerModel(
            facts=self.state.facts,
            submission_in_flight=self.state.submission_in_flight,
            read_only=self.state.read_only,
            pending_images=len(self._attachments),
        )

    @property
    def attachments(self) -> tuple[ImageInput, ...]:
        return tuple(self._attachments)

    def attach(self, image: ImageInput) -> None:
        self._attachments.append(image)
        self._dirty = True

    def clear_attachments(self) -> None:
        self._attachments.clear()
        self._dirty = True

    def activity(self) -> str:
        """The live progress of the open turn, from its recorded start time."""
        facts = self.state.facts
        running = facts.turn_open or facts.server_turn is ServerTurn.RUNNING
        if not running or self.state.turn_started_at <= 0:
            return f"turn:{self.state.turn}" if self.state.turn else ""
        elapsed = max(0.0, self._clock() - self.state.turn_started_at)
        return f"turn:{self.state.turn} {elapsed:.1f}s"

    # --- actions ------------------------------------------------------

    async def connect(self) -> None:
        """Attach, then show what was adopted."""
        try:
            await self.transport.connect()
        finally:
            await self.flush()

    async def run(self) -> None:
        """Read the event stream until the transport stops, then redraw once."""
        try:
            await self.transport.run()
        finally:
            await self.flush()

    async def watch(self) -> None:
        await self.transport.watch()

    async def submit(self, text: str) -> str:
        """Send the composer contents, with whatever is attached to them."""
        images = list(self._attachments)
        input_id = await self.transport.submit(
            text,
            delivery=composer_delivery(self.composer_model()),
            images=images,
        )
        # The submission consumed the attachments; a failure is reported
        # visibly, and the user re-attaches if they retry.
        self._attachments.clear()
        # Sending is an explicit return to the live conversation.  In
        # particular, a tall multi-line composer can shrink the transcript and
        # make Textual report that it is no longer at the end just before the
        # input is submitted.  That layout artefact must not leave the reply
        # below the viewport.
        await self.go_to_tail()
        return input_id

    async def switch_session(
        self,
        session_id: str,
        thread_id: str,
        *,
        mode: str = "resume",
    ) -> None:
        """Attach to another session and redraw from its baseline."""
        await self.transport.switch(session_id=session_id, thread_id=thread_id, mode=mode)
        await self.flush()

    async def sessions(self) -> list[Any]:
        """The sessions the server offers, for the picker."""
        return list(await self.transport.list_sessions())

    async def switch_thread(self, thread_id: str) -> None:
        """Attach to another thread of this session (an empty id means the main one)."""
        await self.transport.switch(
            session_id=self.state.session_id, thread_id=thread_id
        )
        await self.flush()

    async def providers(self) -> Any:
        """The provider/model catalogue, for the pickers."""
        return await self.transport.list_providers()

    async def agents(self) -> Any:
        """The agents this thread can switch to."""
        return await self.transport.list_agents()

    async def select_provider(self, name: str, model: str | None = None) -> None:
        await self.transport.select_provider(name, model)
        await self.flush()

    async def select_effort(self, effort: str) -> None:
        await self.transport.select_effort(effort)
        await self.flush()

    async def select_agent(self, name: str) -> None:
        await self.transport.select_agent(name)
        await self.flush()

    async def commands(self) -> list[Any]:
        """The commands the server offers for the attached thread."""
        return list(await self.transport.list_commands())

    async def run_command(self, raw: str) -> Any:
        """Run one server-owned command; the caller shows what it answered."""
        return await self.transport.run_command(raw)

    async def threads(self) -> list[Any]:
        """The threads this session holds, for the picker."""
        return list(await self.transport.list_threads())

    async def interrupt(self) -> None:
        await self.transport.interrupt()
        await self.flush()

    async def respond_permission(
        self,
        request_id: str,
        decision: Literal["allow", "deny"],
        scope: Literal["once", "session"] = "once",
    ) -> None:
        request = self.state.pending_interactions.get(request_id)
        if not isinstance(request, PermissionRequest):
            raise ValueError(f"No pending permission request {request_id!r}")
        await self.transport.respond_permission(
            request_id,
            decision,
            scope,
        )

    async def respond_user_input(self, request_id: str, answer: str) -> None:
        request = self.state.pending_interactions.get(request_id)
        if not isinstance(request, UserInputRequest):
            raise ValueError(f"No pending user-input request {request_id!r}")
        if not answer.strip():
            raise ValueError("User-input answer must be non-empty")
        await self.transport.respond_user_input(request_id, answer)

    async def page_older(self) -> bool:
        """Move the window back, or load the page before it.

        Returns whether the reader's view of the conversation got older: moving
        within what the client holds, or asking the server for the page before
        the oldest entry it holds.
        """
        if await self._view.page_older(self.state):
            await self.flush()
            return True
        loaded = await self.load_older()
        await self.flush()
        return loaded

    async def load_older(self) -> bool:
        """Ask the server for the page before the oldest entry held.

        One page at a time: while a page is in flight the reader's further asks
        are already answered by it, and a failed page is retried on the next ask.
        Nothing is requested when the client holds the beginning.
        """
        older = self.state.older
        if not isinstance(older, HistoryKnown):
            return False
        self.dispatch(OlderHistoryRequested())
        await self.transport.load_older(older.cursor)
        return True

    async def page_newer(self) -> bool:
        moved = await self._view.page_newer(self.state)
        await self.flush()
        return moved

    async def go_to_tail(self) -> None:
        await self._view.go_to_tail(self.state)
        await self.flush()

    async def clear_transcript(self) -> None:
        """Forget the rendered conversation; the next snapshot restores it."""
        self.dispatch(TranscriptCleared())
        await self.flush()


__all__ = ["TuiController", "ViewPort"]
