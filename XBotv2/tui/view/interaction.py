"""Focused text input for an open-ended typed interaction request."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, Static


INTERACTION_INPUT_CSS = """
InteractionInputScreen {
    align: center middle;
}
InteractionInputScreen > #interaction-input-card {
    width: 72;
    max-width: 90%;
    height: auto;
    background: $panel;
    border: round $accent;
    padding: 0 1;
}
InteractionInputScreen #interaction-input-title {
    height: auto;
    text-style: bold;
}
InteractionInputScreen #interaction-input-question {
    height: auto;
    color: $text-muted;
}
InteractionInputScreen #interaction-input-source,
InteractionInputScreen #interaction-input-hint {
    height: 1;
    color: $text-muted;
}
InteractionInputScreen #interaction-input-hint {
    margin-top: 1;
}
InteractionInputScreen Input {
    height: 1;
    border: none;
    padding: 0;
}
"""


class InteractionInputScreen(ModalScreen[str | None]):
    """Collect one non-empty answer without turning it into a chat message."""

    DEFAULT_CSS = INTERACTION_INPUT_CSS
    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(self, question: str, *, source: str = "") -> None:
        super().__init__()
        self.question = question
        self.source = source

    def compose(self) -> ComposeResult:
        with Vertical(id="interaction-input-card"):
            yield Static("Question", id="interaction-input-title")
            yield Static(self.question, id="interaction-input-question")
            if self.source:
                yield Static(f"Requested by {self.source}", id="interaction-input-source")
            yield Input(placeholder="Type an answer", id="interaction-input")
            yield Static("Enter submit · Esc dismiss", id="interaction-input-hint")

    def on_mount(self) -> None:
        self.query_one("#interaction-input", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        answer = event.value.strip()
        if answer:
            self.dismiss(answer)

    def action_cancel(self) -> None:
        self.dismiss(None)


__all__ = ["INTERACTION_INPUT_CSS", "InteractionInputScreen"]
