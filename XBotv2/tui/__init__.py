"""XBotv2 terminal UI.

This package was rewritten from scratch on ``dev-tui-rewrite``: the previous
``client``, ``textual_client``, ``textual_widgets``, ``terminal`` and chrome
modules were deleted rather than migrated, and every module here is new.

Module boundaries, in dependency order::

    status / timeline          pure data and derivation, no Textual, no IO
    events / state             typed inputs and the reducer over them
    protocol / transport       wire envelope validation and the SSE reader
    view/                      Textual widgets, driven by state snapshots
    controller / app           assembly only

This module stays import-light on purpose: importing the package must not pull
Textual in, so the CLI can validate its arguments without the UI dependency.
"""

__all__: list[str] = []
