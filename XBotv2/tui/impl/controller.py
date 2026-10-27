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
from dataclasses import replace
from pydantic import BaseModel
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Literal, Protocol, Sequence

from XBotv2.jobs.api.contracts import JobView
from XBotv2.jobs.api.protocol import JobListResponse
from XBotv2.interactions.api.models import UserInputRequest
from XBotv2.permissions.api.models import PermissionRequest
from XBotv2.agentloop.api.models import PendingInputData
from XBotv2.agentloop.api.models import MediaInput
from XBotv2.tui.impl.events import (
    InteractionRequest,
    JobsReplaced,
    LocalNotice,
    TranscriptCleared,
    SnapshotAdopted,
    HistoryReplaced,
    UiEvent,
)
from XBotv2.tui.impl.state import (
    HistoryKnown,
    SessionState,
    reduce,
    _rebuild_from_items,
    _older_from_cursor,
)
from XBotv2.tui.impl.status import ServerTurn, Status, derive
from XBotv2.tui.impl.timeline import AssistantEntry, Delivery, ToolEntry, UserEntry
from XBotv2.tui.impl.transport import TransportConfig, TransportSession
from XBotv2.tui.impl.view.composer import ComposerModel
from XBotv2.tui.impl.view.status_bar import StatusLine, status_line_for

if TYPE_CHECKING:
    from XBotv2.config.api.contracts import PatchPluginConfig, PluginConfigCatalog, PluginConfigScope
    from XBotv2.config.api.protocol import SessionPolicyResponse


class ViewPort(Protocol):
    """What the controller needs from the UI.

    The Textual adapter implements this; a test can record calls instead. It is a
    port, not a second client: it renders what it is given and owns only its own
    scroll position.
    """

    async def render_transcript(
        self, state: SessionState, *, thinking: bool
    ) -> bool: ...

    @property
    def reader_at_end(self) -> bool: ...

    def render_status(self, model: StatusLine) -> None: ...

    def render_jobs(self, jobs: Sequence[JobView]) -> None: ...

    def render_queue(self, items: Sequence[PendingInputData]) -> None: ...

    def render_composer(self, model: ComposerModel) -> None: ...

    async def render_interaction(self, request: InteractionRequest | None) -> None: ...

    async def page_older(self, state: SessionState) -> bool: ...

    async def page_newer(self, state: SessionState) -> bool: ...

    def reveal_loaded_older(self, state: SessionState, boundary_id: str) -> None: ...

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
        self._history_state: SessionState | None = None
        self._history_generation = 0
        self._history_newer = None
        self._history_before_id: str | None = None
        self._history_after_id: str | None = None
        self._live_trimmed = False
        self._view = view
        self._clock = clock
        self._sleep = sleep
        self._workspace = workspace
        self._dirty = True
        self._stopped = False
        # Images waiting for the next message. Composing the next input is the
        # client's business, so they live here rather than in session state.
        self._attachments: list[MediaInput] = []
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
        if isinstance(event, (SnapshotAdopted, HistoryReplaced, TranscriptCleared)):
            self._history_generation += 1
            self._history_state = None
            self._history_newer = None
            self._history_before_id = None
            self._history_after_id = None
            self._live_trimmed = False
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
        await self._view.render_transcript(
            self._history_state or self.state,
            thinking=self._history_state is None and self.thinking_activity_visible()
        )
        self._view.render_status(self.status_model())
        self._view.render_jobs(tuple(self.state.jobs.values()))
        self._view.render_queue(self.state.queue)
        self._view.render_composer(self.composer_model())
        request = next(iter(self.state.pending_interactions.values()), None)
        await self._view.render_interaction(request)
        return True

    def _release_held_pages(self) -> bool:
        """Release completed live entries outside the recent count and byte budget."""
        changed = False
        entries = list(self.state.timeline)
        total = sum(_entry_bytes(entry) for entry in entries)
        for entry in entries:
            if len(self.state.timeline) <= 1:
                break
            if len(self.state.timeline) <= self._config.history_retention and total <= 8 * 1024 * 1024:
                break
            if entry.id == self.state.stream_entry_id:
                continue
            if isinstance(entry, UserEntry) and entry.delivery is Delivery.PENDING:
                continue
            if isinstance(entry, ToolEntry) and entry.status in {"pending", "running"}:
                continue
            self.state.timeline.remove(entry.id)
            total -= _entry_bytes(entry)
            changed = True
        if changed:
            self._live_trimmed = True
        return changed

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
            viewing_history=self._history_state is not None,
            pending_images=len(self._attachments),
        )

    @property
    def attachments(self) -> tuple[MediaInput, ...]:
        return tuple(self._attachments)

    def attach(self, image: MediaInput) -> None:
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

    def thinking_activity_visible(self) -> bool:
        """Whether the transcript should show its transient work indicator.

        This is only a projection of the authoritative turn status and latest
        timeline phase. It is not provider reasoning and never enters history.
        """
        if derive(self.state.facts) is not Status.RUNNING:
            return False
        latest = self.state.timeline.get(self.state.timeline.tail_id())
        if latest is None or isinstance(latest, UserEntry):
            return True
        if isinstance(latest, ToolEntry):
            return latest.status not in {"pending", "running"}
        return False

    # --- actions ------------------------------------------------------

    async def connect(self) -> None:
        """Attach, then show what was adopted."""
        try:
            await self.transport.connect()
            await self._refresh_jobs()
        finally:
            await self.flush()

    async def run(self) -> None:
        """Read the event stream until the transport stops, then redraw once."""
        try:
            await self.transport.run()
        finally:
            await self.flush()

    async def watch(self) -> None:
        await self.transport.watch(self._watchdog_needed)

    def _watchdog_needed(self) -> bool:
        facts = self.state.facts
        return (
            facts.turn_open
            or facts.server_turn is ServerTurn.RUNNING
            or self.state.submission_in_flight
        )

    async def submit(self, text: str, *, delivery: Literal["queue", "steer"]) -> str:
        """Send a composer submission with its explicit keyboard delivery mode."""
        following_tail = self._history_state is None and self._view.reader_at_end
        media = list(self._attachments)
        input_id = await self.transport.submit(
            text,
            delivery=delivery,
            media=media,
        )
        # The submission consumed the attachments; a failure is reported
        # visibly, and the user re-attaches if they retry.
        self._attachments.clear()
        # Keep a reader who deliberately paged back on that entry while the new
        # turn streams off-screen. A reader already following the conversation
        # stays pinned despite the composer shrinking during submission.
        if following_tail:
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
        await self._refresh_jobs()
        await self.flush()

    async def sessions(self, *, cursor: str | None = None):
        """One page of sessions the server offers, for the picker."""
        return await self.transport.list_sessions(cursor=cursor)

    async def switch_thread(self, thread_id: str) -> None:
        """Attach to another thread of this session (an empty id means the main one)."""
        await self.transport.switch(
            session_id=self.state.session_id, thread_id=thread_id
        )
        await self._refresh_jobs()
        await self.flush()

    async def _refresh_jobs(self) -> None:
        """Replace task state after attach; never retain another thread's jobs."""
        try:
            response = await self.transport.list_jobs()
        except Exception as exc:  # noqa: BLE001 — the missing state must be visible
            self.dispatch(JobsReplaced(payload=JobListResponse(
                session_id=self.state.session_id,
                thread_id=self.state.thread_id,
            )))
            self.dispatch(LocalNotice(
                notice_kind="jobs",
                text=f"Could not load background tasks: {exc}",
                level="warning",
            ))
            return
        self.dispatch(JobsReplaced(payload=response))

    async def providers(self) -> Any:
        """The provider/model catalogue, for the pickers."""
        return await self.transport.list_providers()

    async def agents(self) -> Any:
        """The agents this thread can switch to."""
        return await self.transport.list_agents()

    async def session_policy(self) -> SessionPolicyResponse:
        """The authoritative policy projection for the current session."""
        return await self.transport.get_session_policy()

    async def plugin_config(
        self, *, scope: PluginConfigScope = "workspace"
    ) -> PluginConfigCatalog:
        """The producer-declared plugin catalog for one configuration scope."""
        return await self.transport.list_plugin_config(scope=scope)

    async def update_plugin_config(
        self, plugin_id: str, patch: PatchPluginConfig
    ) -> PluginConfigCatalog:
        """Write producer-declared values and return the authoritative catalog."""
        return await self.transport.update_plugin_config(plugin_id, patch)

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

    async def threads(self, *, cursor: str | None = None):
        """One page of this session's threads for the picker."""
        return await self.transport.list_threads(cursor=cursor)

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

    async def _show_history_page(self, *, cursor=None, before_id=None, after_id=None, newer=False) -> bool:
        self._history_generation += 1
        generation = self._history_generation
        try:
            page = await self.transport.history_page(cursor=cursor, before_id=before_id, after_id=after_id)
        except Exception as exc:  # noqa: BLE001 — keep the current page visible
            if generation != self._history_generation:
                return False
            self.dispatch(LocalNotice(notice_kind="history", text=f"Could not load history: {exc}", level="warning"))
            await self.flush()
            return False
        if generation != self._history_generation or not page.items:
            return False
        kept = []
        size = 0
        for item in (page.items if newer else reversed(page.items)):
            item_size = _value_bytes(item)
            if kept and size + item_size > 8 * 1024 * 1024:
                break
            kept.append(item)
            size += item_size
        if not newer:
            kept.reverse()
        truncated = len(kept) < len(page.items)
        self._history_before_id = kept[0].id if truncated and not newer else None
        self._history_after_id = kept[-1].id if truncated and newer else None
        view = replace(self.state, viewing_history=True)
        _rebuild_from_items(view, tuple(kept))
        view.older = _older_from_cursor(page.older_cursor)
        self._history_state = view
        self._history_newer = page.newer_cursor
        self._dirty = True
        await self.flush()
        if generation != self._history_generation:
            return False
        await self._view.go_to_tail(view)
        return True

    async def page_older(self) -> bool:
        view = self._history_state or self.state
        if await self._view.page_older(view):
            await self.flush()
            return True
        if self._history_state is not None:
            if self._history_before_id is not None:
                return await self._show_history_page(before_id=self._history_before_id)
            if not isinstance(view.older, HistoryKnown):
                return False
            return await self._show_history_page(cursor=view.older.cursor)
        if not self._live_trimmed and not isinstance(view.older, HistoryKnown):
            return False
        # Live-only notices and in-flight entries do not name archive records.
        for entry in self.state.timeline:
            if isinstance(entry, UserEntry) and entry.delivery is Delivery.ACCEPTED:
                return await self._show_history_page(before_id=entry.id)
            if isinstance(entry, AssistantEntry) and not entry.streaming and not entry.id.startswith("local:"):
                return await self._show_history_page(before_id=entry.id)
            if isinstance(entry, ToolEntry) and entry.status not in {"pending", "running"}:
                return await self._show_history_page(before_id=entry.id)
        return False

    async def load_older(self) -> bool:
        return await self.page_older()

    async def page_newer(self) -> bool:
        view = self._history_state or self.state
        if await self._view.page_newer(view):
            await self.flush()
            return True
        if self._history_state is not None:
            if self._history_after_id is not None:
                return await self._show_history_page(after_id=self._history_after_id, newer=True)
            if self._history_newer is not None:
                return await self._show_history_page(cursor=self._history_newer, newer=True)
        await self.go_to_tail()
        return True

    async def go_to_tail(self) -> None:
        self._history_generation += 1
        self._history_state = None
        self._history_newer = None
        self._history_before_id = None
        self._history_after_id = None
        self._dirty = True
        await self.flush()
        await self._view.go_to_tail(self.state)

    async def clear_transcript(self) -> None:
        """Forget the rendered conversation; the next snapshot restores it."""
        self.dispatch(TranscriptCleared())
        await self.flush()


def _value_bytes(value: Any) -> int:
    if isinstance(value, str):
        return len(value) * 4
    if isinstance(value, dict):
        return sum(_value_bytes(key) + _value_bytes(item) for key, item in value.items())
    if isinstance(value, (list, tuple)):
        return sum(_value_bytes(item) for item in value)
    if isinstance(value, BaseModel):
        return sum(_value_bytes(getattr(value, name)) for name in type(value).model_fields)
    return 0


def _entry_bytes(entry: Any) -> int:
    return sum(_value_bytes(value) for value in vars(entry).values())


__all__ = ["TuiController", "ViewPort"]
