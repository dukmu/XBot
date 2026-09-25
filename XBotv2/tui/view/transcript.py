"""The transcript view: a mounted window over a timeline, updated by id.

The view holds exactly two pieces of state -- which ids are mounted, and which id
the window is anchored at. Positions are never compiled into numbers that other
code has to keep in step, and nothing outside the window is touched.

An entry is rendered once and afterwards only ever updated *with that same
entry*, so a streamed answer cannot land in another message's widget.

Whether the reader is following the tail is not stored here: it is asked of the
scroll container, which is the only thing that actually knows.
"""

from __future__ import annotations

import asyncio
from typing import Sequence

from textual.containers import VerticalScroll
from textual.message import Message
from textual.widget import Widget
from textual.widgets import Static

from XBotv2.tui.state import (
    HistoryAvailable,
    HistoryFailed,
    HistoryLoading,
    OlderHistory,
    SessionState,
)
from XBotv2.tui.timeline import Entry
from XBotv2.tui.view.entries import BlockVisibility, entry_widget, update_entry_widget
from XBotv2.tui.view.plan import ViewPlan, newer_anchor, older_anchor, plan_window


class TranscriptScroll(VerticalScroll):
    """The scrolling container the transcript is mounted into.

    It reports user scrolls and viewport height changes so the controller can
    re-pin a follower to the bottom after a reflow; it holds no window state.
    """

    can_focus = True

    class Scrolled(Message):
        """Posted after a user scroll; ``at_end`` is the resulting state."""

        def __init__(self, at_end: bool) -> None:
            self.at_end = at_end
            super().__init__()

    class HeightChanged(Message):
        pass


def older_history_label(older: OlderHistory) -> str | None:
    """What the transcript says above its oldest entry, or nothing to say.

    The reader can only act on the part of the conversation the client does not
    hold if it is on screen: that there is more, that a page is loading, or that
    the last one failed and asking again is worthwhile.
    """
    if isinstance(older, HistoryLoading):
        return "Loading earlier messages…"
    if isinstance(older, HistoryAvailable):
        return "Earlier messages are available — press PageUp to load them"
    if isinstance(older, HistoryFailed):
        return (
            "Earlier messages unavailable — press PageUp to retry: "
            f"{older.message}"
        )
    return None


class TranscriptView:
    """Owns the mounted window over one timeline."""

    def __init__(
        self,
        container: TranscriptScroll,
        *,
        limit: int = 100,
        assistant_label: str = "Assistant",
        visibility: BlockVisibility | None = None,
    ) -> None:
        if limit < 1:
            raise ValueError("transcript window limit must be positive")
        self.container = container
        self.limit = limit
        self.assistant_label = assistant_label
        self.visibility = visibility or BlockVisibility()
        self._widgets: dict[str, Widget] = {}
        self._mounted: tuple[str, ...] = ()
        # The reader's only view of what the client does not hold. It is mounted
        # only while there is something to say, and always ahead of the first
        # entry: entries are inserted relative to other entries, so it stays
        # above the window however far back the reader pages.
        self.older_notice = Static("", id="older-history")
        self._notice_mounted = False
        self._anchor: str | None = None
        self._rendered: dict[str, Entry] = {}
        # Two renders can be asked for at once: the frame loop flushes while a
        # submission flushes. Both would plan from the same mounted set and each
        # mount a widget for the same entry, leaving one orphaned in the DOM
        # forever. Rendering the window is serialized here, where the DOM is owned.
        self._lock = asyncio.Lock()

    # --- state --------------------------------------------------------

    @property
    def mounted_ids(self) -> tuple[str, ...]:
        return self._mounted

    @property
    def anchor(self) -> str | None:
        """The id the window ends at; ``None`` means the tail."""
        return self._anchor

    @property
    def widget_count(self) -> int:
        return len(self._widgets)

    def set_visibility(self, visibility: BlockVisibility) -> bool:
        """Change what the optional blocks show; report whether anything did.

        Every mounted entry is rendered again, because the preference is not part
        of an entry's value: it only changes how that value is shown.
        """
        if visibility == self.visibility:
            return False
        self.visibility = visibility
        self._rendered.clear()
        return True

    @property
    def reader_at_end(self) -> bool:
        """Whether the reader is following the tail -- asked, never remembered."""
        return bool(self.container.is_vertical_scroll_end)

    def widget_for(self, entry_id: str) -> Widget | None:
        return self._widgets.get(entry_id)

    def window(self, state: SessionState) -> ViewPlan:
        """The plan this view would apply for ``state`` right now."""
        return plan_window(
            state.timeline.ids(),
            self._mounted,
            anchor=self._anchor,
            limit=self.limit,
        )

    # --- rendering ----------------------------------------------------

    async def render(self, state: SessionState) -> bool:
        """Bring the mounted window in line with ``state``; report if it moved."""
        async with self._lock:
            return await self._render_locked(state)

    async def _render_locked(self, state: SessionState) -> bool:
        await self._render_older_notice(state)
        ids = state.timeline.ids()
        if self._anchor is not None and self._anchor not in ids:
            # The anchored entry left the timeline (history rewritten, or the
            # state window moved past it). Fall back to the tail, because the
            # only alternative is inventing a position.
            self._anchor = None
        # Read the reader's intent *before* the content grows: once the window is
        # taller than the viewport, "is at the end" is false by definition, and
        # asking afterwards would silently stop following the tail.
        following = self.reader_at_end
        plan = self.window(state)
        changed = await self._apply(state, plan)
        changed = await self._refresh_changed(state, plan) or changed
        if following:
            # The scroll target is only known after the new widgets have been
            # laid out, so the pin happens on the next refresh.
            self._schedule(
                lambda: self.container.scroll_end(animate=False, immediate=True)
            )
        return changed

    async def _render_older_notice(self, state: SessionState) -> None:
        """Show, above the window, what the client knows about older messages."""
        label = older_history_label(state.older)
        if label is None:
            if self._notice_mounted:
                await self.older_notice.remove()
                self._notice_mounted = False
            return
        self.older_notice.update(label)
        if self._notice_mounted:
            return
        first = self._widgets.get(self._mounted[0]) if self._mounted else None
        if first is None:
            await self.container.mount(self.older_notice)
        else:
            await self.container.mount(self.older_notice, before=first)
        self._notice_mounted = True

    async def _apply(self, state: SessionState, plan: ViewPlan) -> bool:
        changed = False
        for entry_id in plan.remove:
            widget = self._widgets.pop(entry_id, None)
            self._rendered.pop(entry_id, None)
            if widget is not None and widget.parent is not None:
                await widget.remove()
                changed = True
        for entry_id in plan.mount:
            entry = state.timeline.get(entry_id)
            if entry is None:
                continue
            widget = entry_widget(
                entry,
                assistant_label=self.assistant_label,
                visibility=self.visibility,
            )
            self._widgets[entry_id] = widget
            self._rendered[entry_id] = entry
            reference = self._reference_widget(plan.mounted, entry_id)
            if reference is not None:
                await self.container.mount(widget, before=reference)
            else:
                await self.container.mount(widget)
            changed = True
        self._mounted = plan.mounted
        return changed

    async def _refresh_changed(self, state: SessionState, plan: ViewPlan) -> bool:
        """Update entries in place, but only the ones whose value actually moved.

        A streamed answer grows every frame; an entry that has not changed is not
        re-rendered at all, which is what keeps a long transcript from being
        re-parsed for no reason.
        """
        changed = False
        for entry_id in plan.mounted:
            entry = state.timeline.get(entry_id)
            if entry is None or self._rendered.get(entry_id) == entry:
                continue
            widget = self._widgets.get(entry_id)
            if widget is None:
                continue
            applied = await update_entry_widget(
                widget,
                entry,
                assistant_label=self.assistant_label,
                visibility=self.visibility,
            )
            if not applied:
                # The widget has not composed yet; leave it un-recorded so the
                # next render retries instead of losing the update.
                continue
            self._rendered[entry_id] = entry
            changed = True
        return changed

    def _reference_widget(self, mounted: Sequence[str], entry_id: str) -> Widget | None:
        """The next mounted widget, so an entry can be inserted in order."""
        seen = False
        for candidate in mounted:
            if candidate == entry_id:
                seen = True
                continue
            if not seen:
                continue
            widget = self._widgets.get(candidate)
            if widget is not None and widget.parent is not None:
                return widget
        return None

    # --- paging -------------------------------------------------------

    async def page_older(self, state: SessionState) -> bool:
        """Move the window one page towards the oldest entry."""
        ids = state.timeline.ids()
        current = self.window(state).mounted
        if not current:
            return False
        anchor = older_anchor(ids, current)
        if anchor is None:
            return False
        previous_first = current[0]
        self._anchor = anchor
        async with self._lock:
            await self._render_locked(state)
        self._keep_reader_place(state, previous_first)
        return True

    async def page_newer(self, state: SessionState) -> bool:
        """Move the window one page towards the newest entry, or to the tail."""
        ids = state.timeline.ids()
        current = self.window(state).mounted
        if not current:
            return False
        anchor = newer_anchor(ids, current, limit=self.limit)
        if anchor is None:
            if self._anchor is None:
                return False
            self._anchor = None
        else:
            self._anchor = anchor
        async with self._lock:
            await self._render_locked(state)
        return True

    async def go_to_tail(self, state: SessionState) -> None:
        self._anchor = None
        async with self._lock:
            await self._render_locked(state)
        # This is a command, not an incidental render.  Its contract is to put
        # the reader at the live tail even when the mounted window was already
        # the newest one and only a surrounding layout change moved the
        # viewport.
        self._schedule(
            lambda: self.container.scroll_end(animate=False, immediate=True)
        )

    def _keep_reader_place(self, state: SessionState, anchor_entry: str) -> None:
        """After paging up, hold the entry that was at the top of the viewport.

        Anchoring that entry to the top needs no height arithmetic, so it cannot
        drift the way a measured compensation did.
        """
        widget = self._widgets.get(anchor_entry)
        if widget is None:
            return
        self._schedule(
            lambda: self.container.scroll_to_widget(
                widget, top=True, animate=False, immediate=True
            )
        )

    def _schedule(self, callback) -> None:
        app = getattr(self.container, "app", None)
        schedule = getattr(app, "call_after_refresh", None)
        if callable(schedule):
            schedule(callback)
        else:  # pragma: no cover - only before the widget is attached to an app
            callback()


__all__ = ["TranscriptScroll", "TranscriptView"]
