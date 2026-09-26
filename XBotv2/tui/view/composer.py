"""The composer: one hint line, one input, and explicit queue/steer keys.

The hint is a pure function of the facts (``composer_hint``), so the wording can
be checked without a terminal. Two things it must get right:

* while a turn runs, Enter queues and Ctrl+Enter/Alt+S explicitly steer;
* a blocking prompt outranks the running hint, because that is what the user has
  to act on.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Literal

from textual.containers import Horizontal, Vertical
from textual.events import Key
from textual.widgets import Static, TextArea

from XBotv2.tui.status import Connection, Interaction, Interrupt, ServerTurn, StatusFacts

InputDelivery = Literal["queue", "steer"]
SubmitCallback = Callable[[str, InputDelivery], Awaitable[None] | None]

COMPOSER_CSS = """
Composer {
    height: auto;
    width: 1fr;
}
Composer > .composer-hint {
    height: auto;
    width: 1fr;
    padding: 0 1;
}
Composer > .composer-row {
    height: auto;
    width: 1fr;
}
Composer .composer-prompt {
    height: 1;
    width: 1;
    color: $text;
    text-style: bold;
}
Composer #composer-input {
    height: auto;
    min-height: 3;
    max-height: 12;
    width: 1fr;
}
"""

_DEFAULT_HINT = ""
_READ_ONLY_HINT = "viewing another thread — read-only"


@dataclass(frozen=True)
class ComposerModel:
    """Everything the hint depends on."""

    facts: StatusFacts
    submission_in_flight: bool = False
    read_only: bool = False
    # Images waiting to be sent with the next message. They make an otherwise
    # empty composer submittable.
    pending_images: int = 0


def composer_enabled(model: ComposerModel) -> bool:
    return not model.read_only


def composer_can_submit(model: ComposerModel, *, text: str) -> bool:
    """Whether the current composer contents are worth sending."""
    if not composer_enabled(model):
        return False
    return bool(text.strip()) or model.pending_images > 0


def _attachment_note(count: int) -> str:
    return f"{count} image{'s' if count != 1 else ''} attached"


def composer_hint(model: ComposerModel) -> str:
    facts = model.facts
    running = facts.server_turn is ServerTurn.RUNNING or facts.turn_open
    if model.read_only:
        return _READ_ONLY_HINT
    if facts.interaction is Interaction.PERMISSION:
        return "Approval required — use /approve ID [once|session] or /deny ID"
    if facts.interaction is Interaction.USER_INPUT:
        return "Answer required — use /answer ID <text>"
    if facts.interrupt is Interrupt.REQUESTED:
        return "Interrupting…"
    if model.submission_in_flight:
        return "Sending…"
    if model.pending_images > 0 and running:
        return (
            f"{_attachment_note(model.pending_images)} — Enter queues · "
            "Ctrl+Enter/Alt+S steer"
        )
    if model.pending_images > 0:
        return f"{_attachment_note(model.pending_images)} — Enter sends"
    if running:
        return ""
    return _DEFAULT_HINT


def composer_placeholder(model: ComposerModel) -> str:
    facts = model.facts
    running = facts.server_turn is ServerTurn.RUNNING or facts.turn_open
    if model.read_only:
        return "read-only"
    if facts.interaction is Interaction.PERMISSION:
        return "/approve ID | /deny ID"
    if facts.interaction is Interaction.USER_INPUT:
        return "/answer ID <text>"
    return ""


class ComposerInput(TextArea):
    """The text area: Enter sends, Shift+Enter starts a new line."""

    DEFAULT_CSS = COMPOSER_CSS

    def __init__(
        self,
        *,
        on_submit: SubmitCallback,
        id: str | None = None,
        completion: Any = None,
    ) -> None:
        super().__init__("", id=id, soft_wrap=True, show_line_numbers=False, compact=True)
        self.on_submit = on_submit
        # An image-only message is legitimate; the model decides.
        self.may_submit_empty = False
        self.completion = completion

    async def _on_key(self, event: Key) -> None:
        completion = self.completion
        if completion is not None and completion.visible:
            if event.key == "tab":
                event.stop()
                event.prevent_default()
                accepted = completion.accept()
                if accepted is not None:
                    self.load_text(accepted)
                    self.cursor_location = (0, len(accepted))
                return
            if event.key in {"up", "down"}:
                event.stop()
                event.prevent_default()
                completion.move(-1 if event.key == "up" else 1)
                return
            if event.key == "escape":
                event.stop()
                event.prevent_default()
                completion.dismiss()
                return
        if event.key in {"ctrl+enter", "alt+s"}:
            event.stop()
            event.prevent_default()
            await self.submit(delivery="steer")
            return
        if event.key == "enter":
            event.stop()
            event.prevent_default()
            await self.submit(delivery="queue")
            return
        if event.key == "shift+enter":
            event.stop()
            event.prevent_default()
            self.insert("\n")
            return
        await super()._on_key(event)

    async def submit(self, *, delivery: InputDelivery) -> bool:
        """Hand the line to the app.

        Whether it may be *sent* is not decided here: a read-only thread view
        still runs client commands, and only the caller knows which is which.
        """
        text = self.text
        if not text.strip() and not self.may_submit_empty:
            return False
        self.load_text("")
        result = self.on_submit(text, delivery)
        if result is not None:
            await result
        return True


class Composer(Vertical):
    """A hint line and the input it describes."""

    DEFAULT_CSS = COMPOSER_CSS

    def __init__(
        self,
        *,
        on_submit: SubmitCallback,
        id: str | None = None,
        completion: Any = None,
    ) -> None:
        super().__init__(id=id)
        self._on_submit = on_submit
        self._hint_text = ""
        self._hint = Static("", classes="composer-hint")
        self._prompt = Static("❯", classes="composer-prompt")
        self._input = ComposerInput(
            on_submit=on_submit, id="composer-input", completion=completion
        )

    def compose(self):
        yield self._hint
        with Horizontal(classes="composer-row"):
            yield self._prompt
            yield self._input

    @property
    def hint_text(self) -> str:
        """The hint as written, not as Textual happens to store it."""
        return self._hint_text

    @property
    def input(self) -> ComposerInput:
        return self._input

    @property
    def text(self) -> str:
        return self._input.text

    def load_text(self, text: str) -> None:
        self._input.load_text(text)

    def focus(self, *args, **kwargs) -> None:
        self._input.focus(*args, **kwargs)

    def show(self, model: ComposerModel) -> None:
        """Apply a model: hint, placeholder, and whether input is accepted."""
        self._hint_text = composer_hint(model)
        self._hint.update(self._hint_text)
        self._hint.display = bool(self._hint_text)
        self._input.placeholder = composer_placeholder(model)
        self._input.may_submit_empty = model.pending_images > 0


__all__ = [
    "COMPOSER_CSS",
    "Composer",
    "ComposerInput",
    "ComposerModel",
    "composer_can_submit",
    "composer_enabled",
    "composer_hint",
    "composer_placeholder",
]
