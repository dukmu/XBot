"""Context-sensitive keyboard hints below the runtime status line."""

from __future__ import annotations

from rich.text import Text
from textual.widgets import Static

from XBotv2.tui.status import Interaction, Interrupt, ServerTurn
from XBotv2.tui.view.composer import ComposerModel

FOOTER_CSS = """
FooterBar {
    height: 1;
    width: 1fr;
    padding: 0 1;
    background: $panel;
    color: $text-muted;
}
"""


def footer_hint_text(model: ComposerModel) -> str:
    """Name the actions available in the current interaction state."""
    facts = model.facts
    if model.read_only:
        return "Esc main · Ctrl+T agents · PageUp/PageDown history · Ctrl+C copy/quit"
    if facts.interaction is not Interaction.NONE:
        return "? shortcuts · Ctrl+C copy/quit · Ctrl+P commands"
    if facts.interrupt is Interrupt.REQUESTED:
        return "Waiting for interrupt · ? shortcuts · Ctrl+C copy/quit"
    if facts.server_turn is ServerTurn.RUNNING or facts.turn_open:
        return (
            "Esc interrupt · Enter queues · Alt+S steer · ? shortcuts"
        )
    return "? for shortcuts · Ctrl+P commands · Ctrl+T agents · F2 settings"


def render_footer_hints(model: ComposerModel, *, width: int) -> Text:
    """Render one clipped line, preserving Rich's terminal-cell accounting."""
    result = Text(footer_hint_text(model), style="dim")
    result.truncate(max(width, 0), overflow="ellipsis")
    return result


class FooterBar(Static):
    DEFAULT_CSS = FOOTER_CSS

    def show(self, model: ComposerModel, *, width: int | None = None) -> None:
        self.update(render_footer_hints(model, width=width or self.size.width or 80))


__all__ = ["FOOTER_CSS", "FooterBar", "footer_hint_text", "render_footer_hints"]
