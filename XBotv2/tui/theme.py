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
#jobs, #queue {
    height: auto;
    max-height: 8;
}
"""

__all__ = ["TUI_CSS"]
