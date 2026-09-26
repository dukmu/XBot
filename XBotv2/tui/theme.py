"""App-level layout.

Widget-local styles stay with their widget as ``DEFAULT_CSS`` -- that is Textual's
idiom and keeps a widget's appearance next to the widget. What lives here is the
screen layout, which no single widget owns.
"""

from __future__ import annotations

TUI_CSS = """
Screen {
    layout: vertical;
}
#transcript {
    height: 1fr;
    width: 1fr;
    padding: 0 1;
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
    border-top: solid $primary-darken-2;
    color: $text-muted;
    display: none;
}
#composer {
    padding: 0 1;
    border-top: solid $primary-darken-2;
    border-bottom: solid $primary-darken-2;
    background: $panel;
}
#status {
    background: $panel;
    color: $text-muted;
}
#footer {
    height: 1;
    width: 1fr;
    padding: 0 1;
    background: $panel;
    color: $text-muted;
}
"""

__all__ = ["TUI_CSS"]
