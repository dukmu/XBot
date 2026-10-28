"""Inline interactions report results without replacing the conversation screen."""

from textual.app import App, ComposeResult
from textual.widgets import Static

from XBotv2.tui.impl.view.interaction import InteractionInputPanel
from XBotv2.tui.impl.view.settings import SettingsPanel
from XBotv2.config.api.resources import ConfigurationCatalog


class PanelHarness(App):
    def __init__(self, panel):
        super().__init__()
        self.panel = panel
        self.results = []
        panel.on_result = self.results.append

    def compose(self) -> ComposeResult:
        yield Static("Conversation", id="conversation")
        yield self.panel


async def test_question_accepts_input_and_keeps_conversation_visible():
    panel = InteractionInputPanel("What next?", source="agent")
    app = PanelHarness(panel)
    async with app.run_test(size=(60, 20)) as pilot:
        await pilot.press("h", "i", "enter")
        assert app.results == ["hi"]
        assert app.query_one("#conversation").region.height > 0
        assert app.screen is not panel


async def test_settings_escape_reports_close():
    async def read(*args, **kwargs):
        raise AssertionError("empty catalog must not load a document")

    panel = SettingsPanel(ConfigurationCatalog(resources=()), read=read, save=read)
    app = PanelHarness(panel)
    async with app.run_test(size=(60, 20)) as pilot:
        await pilot.pause()
        await pilot.press("escape")
        assert app.results == [None]
        assert app.query_one("#conversation").region.height > 0
