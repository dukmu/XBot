from rich.color import Color as RichColor
from rich.segment import Segment
from rich.style import Style
from textual.color import Color

from XBotv2.commands.impl.plugin import CommandsService
from XBotv2.tui.impl.config import TextualTuiConfig
from XBotv2.tui.impl.terminal import TextualTerminalClient


def test_no_color_keeps_terminal_default_foreground_and_background(monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    client = TextualTerminalClient(
        backend=object(), commands=CommandsService(ownership="caller"),
        config=TextualTuiConfig(),
    )
    default = RichColor.parse("default")
    segments = [Segment("conversation", Style.from_color(default, default))]
    for line_filter in client.app.get_line_filters():
        segments = line_filter.apply(segments, Color.parse("ansi_default"))
    style = segments[0].style
    assert style.color == default
    assert style.bgcolor == default
    assert "30;40" not in style.render("conversation")
