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
from typing import Any, Awaitable, Callable, Protocol, Sequence

from XBotv2.jobs.contracts import JobSnapshot
from XBotv2.session.contracts import PendingInputData
from XBotv2.session.contracts import ImageInput
from XBotv2.tui.events import TranscriptCleared, UiEvent
from XBotv2.tui.state import SessionState, reduce
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

    def render_status(self, model: StatusLine) -> None: ...

    def render_jobs(self, jobs: Sequence[JobSnapshot]) -> None: ...

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
        """Redraw the views if anything changed since the last flush."""
        if not self._dirty:
            return False
        self._dirty = False
        await self._view.render_transcript(self.state)
        self._view.render_status(self.status_model())
        self._view.render_jobs(tuple(self.state.jobs.values()))
        self._view.render_queue(self.state.queue)
        self._view.render_composer(self.composer_model())
        return True

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
        await self.flush()
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

    async def page_older(self) -> bool:
        moved = await self._view.page_older(self.state)
        await self.flush()
        return moved

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
