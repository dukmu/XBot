"""App-level layout.

Widget-local styles stay with their widget as ``DEFAULT_CSS`` -- that is Textual's
idiom and keeps a widget's appearance next to the widget. What lives here is the
screen layout, which no single widget owns.
"""

from __future__ import annotations

TUI_CSS = """
* {
    scrollbar-size: 1 1;
    scrollbar-color: ansi_bright_black;
    scrollbar-color-hover: ansi_white;
    scrollbar-color-active: ansi_white;
    scrollbar-background: ansi_default;
    scrollbar-background-hover: ansi_default;
    scrollbar-background-active: ansi_default;
    scrollbar-corner-color: ansi_default;
}
Screen {
    layout: vertical;
}
Screen:inline {
    height: 24;
    max-height: 100%;
}
#transcript {
    height: 1fr;
    width: 1fr;
    padding: 0 1;
}
#inline-panel {
    height: auto;
    max-height: 60%;
    width: 1fr;
    display: none;
}
#completion {
    height: auto;
    max-height: 10;
}
#panels {
    height: auto;
    max-height: 8;
    display: none;
}
#panels.visible {
    display: block;
}
#jobs {
    height: auto;
    max-height: 8;
}
#job-panel {
    width: 1fr;
    height: auto;
    padding: 0;
    border-top: none;
}
#queue {
    height: auto;
    max-height: 4;
    width: 1fr;
    padding: 0 1;
    color: $text-muted;
    display: none;
}
#composer {
    padding: 0 1;
    border-top: solid $text-muted;
    background: $background;
}
#status {
    background: $background;
    color: $text-muted;
}
#footer {
    height: 1;
    width: 1fr;
    padding: 0 1;
    background: $background;
    color: $text-muted;
}
"""

__all__ = ["TUI_CSS"]
