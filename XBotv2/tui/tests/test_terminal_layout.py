from rich.console import Console
from textual.app import App, ComposeResult
from textual.widgets import Static

from XBotv2.tui.impl.theme import TUI_CSS
from XBotv2.tui.impl.view.composer import Composer, ComposerInput
from XBotv2.tui.impl.view.entries import _TranscriptMarkdown


class LayoutApp(App):
    CSS = TUI_CSS

    def compose(self) -> ComposeResult:
        yield Static("conversation", id="transcript")
        yield Composer(on_submit=lambda *_: None, id="composer")
        yield Static("Ready", id="status")
        yield Static("Ctrl+P commands", id="footer")


async def test_compact_composer_and_terminal_background():
    app = LayoutApp()
    app.theme = "ansi-dark"
    async with app.run_test(size=(80, 24)) as pilot:
        editor = app.query_one(ComposerInput)
        editor.focus()
        await pilot.pause()
        assert editor.size.height == 1
        assert editor.styles.border.top[0] == ""
        assert editor.styles.border.bottom[0] == ""
        assert app.query_one("#composer").styles.border.top[0] == "solid"
        assert app.query_one("#composer").styles.border.bottom[0] == ""
        assert app.query_one("#transcript").size.height > editor.size.height


def test_code_uses_terminal_syntax_without_background():
    markdown = _TranscriptMarkdown("```python\nprint(1)\n```")
    segments = list(Console(width=60).render(markdown))
    assert any("print" in segment.text and segment.style.color for segment in segments)
    assert all(segment.style is None or segment.style.bgcolor is None for segment in segments)
