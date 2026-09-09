"""TUI interaction modes.

Single source of truth for the high-level mode the TUI is in. The render log
and the composer consult this module instead of recomputing state from
scattered predicates.

See ``XBotv2/.agents/skills/xbot-plugin-development`` for the current client
boundary and interaction contract.
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
