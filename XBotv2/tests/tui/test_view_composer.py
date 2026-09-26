"""The composer: what it says, and what Enter/Ctrl+Enter do.

The hint is a pure function of the facts, so the wording can be checked without a
terminal. These tests keep the displayed delivery intent aligned with each key.
"""

from __future__ import annotations

from textual.events import Paste
from textual.app import App, ComposeResult
from textual.widgets import Static

from XBotv2.tui.status import Connection, Interaction, Interrupt, ServerTurn, StatusFacts
from XBotv2.tui.view.composer import (
    Composer,
    ComposerModel,
    composer_can_submit,
    composer_enabled,
    composer_hint,
    composer_placeholder,
)


def model(**overrides) -> ComposerModel:
    fields = {
        "facts": StatusFacts(connection=Connection.CONNECTED),
        "submission_in_flight": False,
        "read_only": False,
    }
    fields.update(overrides)
    return ComposerModel(**fields)


# --- what it says ---------------------------------------------------------


def test_an_idle_thread_does_not_spend_a_transcript_row_on_key_help() -> None:
    assert composer_hint(model()) == ""


def test_a_running_turn_keeps_delivery_help_in_the_footer() -> None:
    hint = composer_hint(model(facts=StatusFacts(connection=Connection.CONNECTED, server_turn=ServerTurn.RUNNING)))
    assert hint == ""


def test_a_running_turn_is_recognised_from_the_server_answer_alone() -> None:
    hint = composer_hint(
        model(facts=StatusFacts(connection=Connection.CONNECTED, turn_open=False, server_turn=ServerTurn.RUNNING))
    )
    assert hint == ""


def test_a_pending_approval_asks_for_a_decision() -> None:
    hint = composer_hint(
        model(facts=StatusFacts(connection=Connection.CONNECTED, interaction=Interaction.PERMISSION))
    )
    assert "/approve" in hint.lower()
    assert "deny" in hint.lower()


def test_a_pending_question_asks_for_an_answer() -> None:
    hint = composer_hint(
        model(facts=StatusFacts(connection=Connection.CONNECTED, interaction=Interaction.USER_INPUT))
    )
    assert "answer" in hint.lower()


def test_an_in_flight_submission_says_it_is_sending() -> None:
    assert "sending" in composer_hint(model(submission_in_flight=True)).lower()


def test_an_interrupt_in_flight_says_so() -> None:
    hint = composer_hint(
        model(facts=StatusFacts(connection=Connection.CONNECTED, interrupt=Interrupt.REQUESTED))
    )
    assert "interrupt" in hint.lower()


def test_a_read_only_view_says_so() -> None:
    assert "read-only" in composer_hint(model(read_only=True)).lower()
    assert composer_enabled(model(read_only=True)) is False


def test_a_pending_approval_outranks_the_running_hint() -> None:
    """The blocking question is what the user must act on."""
    hint = composer_hint(
        model(
            facts=StatusFacts(
                connection=Connection.CONNECTED,
                server_turn=ServerTurn.RUNNING,
                interaction=Interaction.PERMISSION,
            )
        )
    )
    assert "steer" not in hint.lower()


def test_the_placeholder_tracks_the_same_state() -> None:
    assert composer_placeholder(
        model(facts=StatusFacts(connection=Connection.CONNECTED, server_turn=ServerTurn.RUNNING))
    ) == ""
    assert "/approve" in composer_placeholder(
        model(facts=StatusFacts(connection=Connection.CONNECTED, interaction=Interaction.PERMISSION))
    ).lower()
    assert composer_placeholder(model(read_only=True)).lower().startswith("read-only")


# --- what Enter does ------------------------------------------------------


class Harness(App[None]):
    def __init__(self) -> None:
        super().__init__()
        self.submitted: list[str] = []
        self.deliveries: list[str] = []
        self.model = model()

    def compose(self) -> ComposeResult:
        yield Composer(id="input", on_submit=self.submit)
        yield Static(id="hint")

    def on_mount(self) -> None:
        self.composer = self.query_one("#input", Composer)
        self.composer.show(self.model)
        self.composer.focus()

    async def submit(self, text: str, delivery: str) -> None:
        self.submitted.append(text)
        self.deliveries.append(delivery)


async def test_enter_submits_and_clears_the_composer() -> None:
    app = Harness()
    async with app.run_test() as pilot:
        app.composer.load_text("hello")
        await pilot.press("enter")
        await pilot.pause()
        assert app.submitted == ["hello"]
        assert app.deliveries == ["queue"]
        assert app.composer.text == ""


async def test_ctrl_enter_submits_as_an_explicit_steer() -> None:
    app = Harness()
    async with app.run_test() as pilot:
        app.composer.load_text("change direction")
        await pilot.press("ctrl+enter")
        await pilot.pause()
        assert app.submitted == ["change direction"]
        assert app.deliveries == ["steer"]
        assert app.composer.text == ""


async def test_alt_s_is_the_terminal_fallback_for_explicit_steer() -> None:
    app = Harness()
    async with app.run_test() as pilot:
        app.composer.load_text("change direction")
        await pilot.press("alt+s")
        await pilot.pause()
        assert app.submitted == ["change direction"]
        assert app.deliveries == ["steer"]


async def test_shift_enter_adds_a_line_instead_of_submitting() -> None:
    app = Harness()
    async with app.run_test() as pilot:
        app.composer.load_text("first")
        await pilot.press("shift+enter")
        await pilot.press("a")
        await pilot.pause()
        assert app.submitted == []
        assert "\n" in app.composer.text


async def test_multiline_paste_preserves_unicode_and_trailing_newline_until_submit() -> None:
    app = Harness()
    pasted = "第一行\nsecond line 🌱\n"
    async with app.run_test() as pilot:
        app.post_message(Paste(pasted))
        await pilot.pause()
        assert app.composer.text == pasted
        assert app.submitted == []

        await pilot.press("enter")
        await pilot.pause()

        assert app.submitted == [pasted]
        assert app.composer.text == ""


async def test_an_empty_composer_submits_nothing() -> None:
    app = Harness()
    async with app.run_test() as pilot:
        app.composer.load_text("   ")
        await pilot.press("enter")
        await pilot.pause()
        assert app.submitted == []


async def test_a_read_only_view_still_hands_commands_to_the_app() -> None:
    """Read-only is a rule about messages, not about typing.

    Leaving a read-only thread view is itself a command, so the widget must not
    swallow the line; the app decides what may be sent.
    """
    app = Harness()
    async with app.run_test() as pilot:
        app.composer.show(model(read_only=True))
        assert "read-only" in app.composer.hint_text.lower()
        app.composer.load_text("/thread main")
        await pilot.press("enter")
        await pilot.pause()
        assert app.submitted == ["/thread main"]


async def test_showing_a_model_updates_the_hint() -> None:
    app = Harness()
    async with app.run_test() as pilot:
        app.composer.show(model(read_only=True))
        await pilot.pause()
        assert "read-only" in app.composer.hint_text.lower()


# --- pending attachments change what can be sent -------------------------


def test_the_hint_reports_pending_attachments() -> None:
    hint = composer_hint(model(pending_images=1))
    assert "1 image" in hint


def test_a_running_turn_keeps_delivery_keys_visible_with_an_attachment() -> None:
    hint = composer_hint(
        model(
            pending_images=1,
            facts=StatusFacts(connection=Connection.CONNECTED, server_turn=ServerTurn.RUNNING),
        )
    )
    assert "Enter queues" in hint
    assert "Ctrl+Enter/Alt+S steer" in hint


def test_the_placeholder_reports_pending_attachments() -> None:
    assert composer_placeholder(model(pending_images=1)) == ""
    assert composer_placeholder(model(pending_images=2)) == ""


def test_an_empty_composer_may_still_submit_an_attachment() -> None:
    assert composer_can_submit(model(pending_images=1), text="") is True
    assert composer_can_submit(model(), text="") is False
    assert composer_can_submit(model(), text="  ") is False
    assert composer_can_submit(model(), text="hello") is True


def test_a_read_only_view_never_submits_an_attachment() -> None:
    assert composer_can_submit(model(read_only=True, pending_images=1), text="") is False


async def test_enter_sends_an_image_with_no_text() -> None:
    app = Harness()
    async with app.run_test() as pilot:
        app.composer.show(model(pending_images=1))
        await pilot.press("enter")
        await pilot.pause()
        assert app.submitted == [""], "an attached image is a message on its own"
        assert app.deliveries == ["queue"]


async def test_enter_still_refuses_an_empty_composer_with_no_image() -> None:
    app = Harness()
    async with app.run_test() as pilot:
        app.composer.show(model())
        await pilot.press("enter")
        await pilot.pause()
        assert app.submitted == []
