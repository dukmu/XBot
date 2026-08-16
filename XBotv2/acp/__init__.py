"""Agent Client Protocol adapter for XBot.

The Agent Client Protocol SDK is installed as a top-level package named
``acp`` (dependency ``agent-client-protocol>=0.11.1,<0.12``) — the same name
as this package in the flat layout, so this package shadows the SDK.  To keep
both working, this package re-exports the SDK's public API (its package
``__init__`` is executed into this namespace and this package's ``__path__``
is extended with the SDK's directory, first) alongside the XBot adapter
itself (:class:`XBotACPAgent` / :func:`run_acp`).
"""

from __future__ import annotations

import sys
from pathlib import Path


def _sdk_init() -> Path | None:
    """Locate the installed SDK's package init (anywhere but this dir)."""
    here = Path(__file__).resolve().parent
    for entry in sys.path:
        if not entry:
            continue
        candidate = Path(entry).resolve() / "acp" / "__init__.py"
        if candidate.parent == here:
            continue
        if candidate.is_file():
            return candidate
    return None


_sdk_init_path = _sdk_init()
if _sdk_init_path is not None:
    __path__.insert(0, str(_sdk_init_path.parent))  # type: ignore[attr-defined]
    exec(
        compile(
            _sdk_init_path.read_text(encoding="utf-8"),
            str(_sdk_init_path),
            "exec",
        ),
        globals(),
    )

from acp.xbot_agent import XBotACPAgent  # noqa: E402
from acp.server import run_acp  # noqa: E402

__all__ = [*globals().get("__all__", []), "XBotACPAgent", "run_acp"]
