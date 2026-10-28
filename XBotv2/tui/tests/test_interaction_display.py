import pytest
from textual.app import App, ComposeResult
from textual.widgets import Static

from XBotv2.tui.impl.status import Connection, ServerTurn, StatusFacts
from XBotv2.tui.impl.timeline import NoticeEntry
from XBotv2.tui.impl.view.composer import ComposerModel
from XBotv2.tui.impl.view.entries import entry_widget, update_entry_widget
from XBotv2.tui.impl.view.footer import render_footer_hints


@pytest.mark.parametrize("kind,prompt,pending,resolved", [
    ("permission", "shell: permission needed to run command", "? Permission required · shell", "shell: permission needed to run command · → deny"),
    ("user_input", "Which branch?\n1. main — Stable\n2. dev — Work", "? Question", "Which branch? · → answered"),
])
async def test_interaction_transcript_stays_compact_and_keeps_outcome(kind, prompt, pending, resolved):
    original = NoticeEntry(id="interaction:123", notice_kind=f"interaction:{kind}", text=f"ID: 123\n{prompt}")
    outcome = NoticeEntry(id=original.id, notice_kind=original.notice_kind, text=f"{original.text}\n→ {'deny' if kind == 'permission' else 'answered'}")
    widget = entry_widget(original, body_renderable=None)

    class InteractionApp(App):
        def compose(self) -> ComposeResult:
            yield widget

    async with InteractionApp().run_test() as pilot:
        await pilot.pause()
        assert len(widget.query(Static)) == 1
        assert str(widget.query_one(Static).content) == pending
        await update_entry_widget(widget, outcome, body_renderable=None, previous=original)
        await pilot.pause()
        shown = str(widget.query_one(Static).content)
        assert resolved in shown
        assert "ID: 123" not in shown
        assert "1. main" not in shown


def test_exit_hint_is_visible_for_idle_and_running():
    for turn in (ServerTurn.IDLE, ServerTurn.RUNNING):
        model = ComposerModel(facts=StatusFacts(connection=Connection.CONNECTED, server_turn=turn))
        assert "Ctrl+C copy/quit" in render_footer_hints(model, width=60).plain
