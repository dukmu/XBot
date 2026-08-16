"""MCP plugin package: connects MCP servers and registers their tools.

The official Model Context Protocol SDK is installed as a top-level package
named ``mcp`` (dependency ``mcp>=1.27,<2``) — the same name as this plugin
directory in the flat layout, so this package shadows the SDK.  To keep both
working, this package re-exports the SDK's public API: the SDK's package
``__init__`` is executed into this namespace and this package's ``__path__``
is extended with the SDK's directory (first), so SDK submodules
(``mcp.client.session``, ``mcp.types``, ...) resolve to the installed SDK
while the plugin's own modules (``mcp.plugin``, ``mcp.mcp_client``,
``mcp.callbacks``, ``mcp.tool``) keep resolving to this directory.
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
        candidate = Path(entry).resolve() / "mcp" / "__init__.py"
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
