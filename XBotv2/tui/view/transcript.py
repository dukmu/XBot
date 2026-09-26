"""The transcript view: a mounted window over a timeline, updated by id.

The view holds the mounted entry ids and which id the window is anchored at.
Positions are never compiled into numbers that other code has to keep in step,
and nothing outside the window is touched. A controller-supplied thinking
activity may be mounted after the timeline tail, but it is transient presentation
and never becomes an entry.

An entry is rendered once and afterwards only ever updated *with that same
entry*, so a streamed answer cannot land in another message's widget.

Whether the reader is following the tail is not stored here: it is asked of the
scroll container, which is the only thing that actually knows.
"""

from __future__ import annotations

import asyncio
from typing import Sequence

from textual.containers import VerticalScroll
from textual.widget import Widget
from textual.widgets import Static

from XBotv2.tui.state import (
    HistoryAvailable,
    HistoryFailed,
    HistoryLoading,
    OlderHistory,
    SessionState,
)
from XBotv2.tui.timeline import AssistantEntry, Entry, ToolEntry
from XBotv2.tui.view.entries import (
    BlockVisibility,
    EntryWidget,
    entry_widget,
    update_entry_widget,
)
from XBotv2.tui.view.plan import ViewPlan, newer_anchor, older_anchor, plan_window


class TranscriptScroll(VerticalScroll):
    """The scrolling container the transcript is mounted into.

    It owns the reader's stable visible-entry anchor. A resize changes wrapping
    and therefore invalidates Textual's numeric scroll offset; the anchor lets
    this container restore the same entry without making the timeline remember
    presentation position.
    """

    can_focus = True

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._reader_anchor: Widget | None = None
        self._tail_follow_pending = False

    @property
    def following_tail(self) -> bool:
        """Whether the reader is at the tail or a post-layout tail pin is due."""
        return self._tail_follow_pending or self.is_vertical_scroll_end

    def watch_scroll_y(self, old_value: float, new_value: float) -> None:
        super().watch_scroll_y(old_value, new_value)
        if old_value == new_value:
            return
        if self.is_vertical_scroll_end:
            self._reader_anchor = None
        else:
            self.call_after_refresh(self._remember_reader_anchor)

    def on_resize(self) -> None:
        anchor = self._reader_anchor
        if anchor is not None and anchor.parent is self:
            self.call_after_refresh(
                lambda: self.scroll_to_widget(
                    anchor, top=True, animate=False, immediate=True
                )
            )
        else:
            self.call_after_refresh(self.scroll_to_tail)

    def _remember_reader_anchor(self) -> None:
        viewport = self.region
        for child in self.children:
            if not isinstance(child, EntryWidget):
                continue
            region = child.region
            if region.y < viewport.bottom and region.bottom > viewport.y:
                self._reader_anchor = child
                return

    def preserve_reader_position(self) -> None:
        """Keep the same transcript row at the same screen coordinate."""
        self._remember_reader_anchor()
        anchor = self._reader_anchor
        if anchor is None or anchor.parent is not self:
            return
        visible_y = anchor.region.y - self.scroll_y

        def restore() -> None:
            if anchor.parent is not self:
                return
            delta = anchor.region.y - self.scroll_y - visible_y
            if delta:
                self.scroll_relative(y=delta, animate=False, immediate=True)

        self.call_after_refresh(restore)

    def scroll_to_tail(self) -> None:
        """Show the tail without preserving an invalid offset after shrink."""
        if self.max_scroll_y <= 0:
            self.scroll_home(animate=False, immediate=True)
        else:
            self.scroll_end(animate=False, immediate=True)

    def follow_tail_after_refresh(self) -> None:
        """Keep follow intent across the layout pass that creates the new tail."""
        self._tail_follow_pending = True
        remaining = 3

        def follow() -> None:
            nonlocal remaining
            self.scroll_to_tail()
            remaining -= 1
            if remaining:
                self.call_after_refresh(follow)
            else:
                self._tail_follow_pending = False

        self.call_after_refresh(follow)

class ThinkingActivity(Static):
    """Transient turn activity; deliberately not a timeline entry."""

    DEFAULT_CSS = """
    ThinkingActivity {
        height: 1;
        width: 1fr;
        margin-bottom: 0;
        padding-left: 1;
        border-left: thick $secondary;
        color: $text-muted;
        text-style: italic;
    }
    """

    def __init__(self) -> None:
        super().__init__("✳ Thinking…", id="thinking-activity")


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
        self._thinking_activity: ThinkingActivity | None = None
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
        return bool(self.container.following_tail)

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

    async def render(self, state: SessionState, *, thinking: bool = False) -> bool:
        """Bring the mounted window in line with ``state``; report if it moved."""
        async with self._lock:
            return await self._render_locked(state, thinking=thinking)

    async def _render_locked(
        self, state: SessionState, *, thinking: bool | None = None
    ) -> bool:
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
        changed = await self._sync_thinking_activity(thinking) or changed
        if following:
            # The scroll target is only known after the new widgets have been
            # laid out. Recompute from the post-layout range: a block collapse
            # may leave the transcript shorter than its viewport.
            self.container.follow_tail_after_refresh()
        else:
            self.container.preserve_reader_position()
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
        plan = self._reuse_transition_widget(state, plan)
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
            elif (
                self._thinking_activity is not None
                and self._thinking_activity.parent is not None
            ):
                await self.container.mount(widget, before=self._thinking_activity)
            else:
                await self.container.mount(widget)
            changed = True
        self._mounted = plan.mounted
        return changed

    async def _sync_thinking_activity(self, thinking: bool | None) -> bool:
        """Mount the controller's transient activity after the timeline tail."""
        if thinking is None:
            return False
        if thinking:
            if (
                self._thinking_activity is not None
                and self._thinking_activity.parent is not None
            ):
                return False
            self._thinking_activity = ThinkingActivity()
            await self.container.mount(self._thinking_activity)
            return True
        if self._thinking_activity is None:
            return False
        activity = self._thinking_activity
        self._thinking_activity = None
        if activity.parent is None:
            return False
        await activity.remove()
        return True

    def _reuse_transition_widget(
        self, state: SessionState, plan: ViewPlan
    ) -> ViewPlan:
        """Keep presentation state when a live row adopts its record id.

        Assistant streams and running tools begin with transient ids and finish
        with durable record ids. That data identity change is not a new visual
        row; rebuilding it causes a visible flash and loses block state.
        """
        if (
            len(plan.remove) != 1
            or len(plan.mount) != 1
            or not self._mounted
            or not plan.mounted
        ):
            return plan

        previous_id = plan.remove[0]
        current_id = plan.mount[0]
        if self._mounted.index(previous_id) != plan.mounted.index(current_id):
            return plan

        previous = self._rendered.get(previous_id)
        current = state.timeline.get(current_id)
        widget = self._widgets.get(previous_id)
        assistant_transition = (
            isinstance(previous, AssistantEntry)
            and previous.streaming
            and isinstance(current, AssistantEntry)
            and not current.streaming
        )
        tool_transition = (
            isinstance(previous, ToolEntry)
            and previous.status in {"pending", "running"}
            and isinstance(current, ToolEntry)
            and current.status not in {"pending", "running"}
            and previous.call_id == current.call_id
        )
        if widget is None or not (assistant_transition or tool_transition):
            return plan

        self._widgets.pop(previous_id)
        self._rendered.pop(previous_id)
        self._widgets[current_id] = widget
        # Preserve the prior value as the diff baseline. The normal refresh
        # below applies the canonical record to this same widget in place.
        self._rendered[current_id] = previous
        return ViewPlan(
            mounted=plan.mounted,
            mount=(),
            remove=(),
            at_tail=plan.at_tail,
        )

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
            self.container.scroll_to_tail
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
