"""Focused text input for an open-ended typed interaction request."""

from __future__ import annotations

from typing import Callable

from textual.app import ComposeResult
from textual.containers import VerticalScroll
from textual.widget import Widget
from textual.widgets import Input, Static


INTERACTION_INPUT_CSS = """
InteractionInputPanel {
    width: 100%;
    height: auto;
    max-height: 100%;
}
InteractionInputPanel > #interaction-input-card {
    width: 100%;
    max-height: 100%;
    height: auto;
    padding: 0 1;
}
InteractionInputPanel #interaction-input-title {
    height: auto;
    text-style: bold;
}
InteractionInputPanel #interaction-input-question {
    height: auto;
    color: $text-muted;
}
InteractionInputPanel #interaction-input-source,
InteractionInputPanel #interaction-input-hint {
    height: 1;
    color: $text-muted;
}
InteractionInputPanel #interaction-input-hint {
    margin-top: 1;
}
InteractionInputPanel Input {
    height: 1;
    border: none;
    padding: 0;
}
"""


class InteractionInputPanel(Widget):
    """Collect one non-empty answer without turning it into a chat message."""

    DEFAULT_CSS = INTERACTION_INPUT_CSS
    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(self, question: str, *, source: str = "") -> None:
        super().__init__()
        self.on_result: Callable[[str | None], None] | None = None
        self.question = question
        self.source = source

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="interaction-input-card"):
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
            self._finish(answer)

    def action_cancel(self) -> None:
        self._finish(None)

    def _finish(self, value: str | None) -> None:
        if self.on_result is not None:
            self.on_result(value)


__all__ = ["INTERACTION_INPUT_CSS", "InteractionInputPanel"]
