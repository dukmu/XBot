"""TUI interaction modes.

Single source of truth for the high-level mode the TUI is in. The render log
and the composer consult this module instead of recomputing state from
scattered predicates.

See ``docsv2/tui_opencode_requirements.md`` §8.
"""

from __future__ import annotations

from enum import Enum


class Mode(str, Enum):
    """High-level TUI interaction states.

    Strings are stable on the wire (TRACE logs) and on disk (future JSON config).
    """

    COMPOSING = "composing"
    RUNNING = "running"
    CHOOSING = "choosing"
    SUBMITTED = "submitted"
    ERROR = "error"


# Visual badge text for the status bar. Keep in sync with §7.1 of the design
# document.
MODE_BADGE: dict[Mode, str] = {
    Mode.COMPOSING: "Ready",
    Mode.RUNNING: "Running",
    Mode.CHOOSING: "Choice",
    Mode.SUBMITTED: "Waiting",
    Mode.ERROR: "Error",
}
