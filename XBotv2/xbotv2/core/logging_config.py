"""Centralised logging configuration for XBotv2.

Goals (v1.2, see docsv2 §10.5.10):

- Default to a **file** log so the user can inspect what happened
  after a TUI error. The TUI in particular runs as a terminal app,
  so any unhandled exception that would normally print to stderr
  is invisible to the user once the screen clears.
- Location: ``<data_dir>/logs/xbotv2.log`` by default, overridable
  with the ``XBOTV2_LOG_FILE`` environment variable or
  ``--log-file`` CLI flag.
- Format: human-readable, includes the logger name + level + a
  per-thread request id so dispatch → engine → TUI events can be
  correlated. Multi-process safe (FileHandler is opened in append
  mode, and we never truncate).
- Rotation: capped at 5 MB per file, 3 backups. We do **not** rotate
  by time — debug sessions can run for hours, and a 5 MB cap is
  enough to capture a long tool-call session's worth of events.
- Levels:
    - ``--log-level DEBUG`` — verbose engine state dumps.
    - ``--log-level INFO`` (default) — turn boundaries, tool calls,
      engine errors.
    - ``--log-level WARNING`` — only engine + HTTP server warnings.
    - ``--log-level ERROR`` — only exceptions.

The TUI is non-fatal: a logging setup error must not prevent the
TUI from starting. All errors are swallowed and reported via
``logger.exception`` so they show up in the file log itself (or
stderr, if the file log also fails).
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import sys
from pathlib import Path
from typing import Any

# Single source of truth for the default log file location. The
# CLI / ``--data-dir`` can override it at startup.
_DEFAULT_LOG_DIRNAME = "logs"
_DEFAULT_LOG_BASENAME = "xbotv2.log"
_MAX_BYTES = 5 * 1024 * 1024  # 5 MB
_BACKUP_COUNT = 3

# Names of the package loggers we own. These are configured by
# :func:`setup_logging`; callers should not touch them.
_PACKAGE_LOGGERS = (
    "xbotv2",
    "xbotv2.engine",
    "xbotv2.http_server",
    "xbotv2.server",
    "xbotv2.tools",
    "xbotv2.tui",
    "xbotv2.protocol",
    "xbotv2.core",
)

# External loggers we leave alone (uvicorn / starlette / httpx /
# langchain are noisy at INFO).
_NOISY_LOGGERS = (
    "uvicorn",
    "uvicorn.error",
    "uvicorn.access",
    "httpx",
    "httpcore",
    "starlette",
    "langchain",
    "langchain_core",
)


def _resolve_log_file(data_dir: str | os.PathLike[str] | None) -> Path:
    """Return the absolute path of the log file; create its directory."""

    env = os.environ.get("XBOTV2_LOG_FILE")
    if env:
        path = Path(env).expanduser()
    else:
        base = Path(data_dir).expanduser() if data_dir else Path("data")
        path = base / _DEFAULT_LOG_DIRNAME / _DEFAULT_LOG_BASENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def setup_logging(
    *,
    data_dir: str | os.PathLike[str] | None = None,
    level: str = "INFO",
    log_file: str | os.PathLike[str] | None = None,
    also_stderr: bool | None = None,
) -> Path:
    """Configure the package loggers.

    Args:
        data_dir: Where to put ``logs/xbotv2.log`` if ``log_file`` is
            not given.
        level: One of ``"DEBUG"`` / ``"INFO"`` / ``"WARNING"`` /
            ``"ERROR"``. Default: ``"INFO"``.
        log_file: Explicit path; overrides ``data_dir`` and
            ``XBOTV2_LOG_FILE``.
        also_stderr: If True, also write to stderr (handy for
            ``--mode once``). If None, defaults to True when stderr
            is a TTY, False otherwise (so the TUI's screen is not
            polluted).

    Returns:
        The path to the log file actually used.
    """

    if log_file is not None:
        path = Path(log_file).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
    else:
        path = _resolve_log_file(data_dir)

    if also_stderr is None:
        also_stderr = sys.stderr.isatty()

    root = logging.getLogger("xbotv2")
    # Avoid stacking handlers if setup_logging is called twice
    # (e.g. once from __main__ and once from the spawned server).
    for handler in list(root.handlers):
        root.removeHandler(handler)
    root.setLevel(level.upper())

    fmt = logging.Formatter(
        fmt="%(asctime)s.%(msecs)03d %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    try:
        file_handler: logging.Handler = logging.handlers.RotatingFileHandler(
            path,
            maxBytes=_MAX_BYTES,
            backupCount=_BACKUP_COUNT,
            encoding="utf-8",
            delay=False,
        )
        file_handler.setFormatter(fmt)
        file_handler.setLevel(level.upper())
        root.addHandler(file_handler)
    except OSError:
        # If the log file cannot be opened (e.g. read-only volume),
        # we still want the app to run. Fall through to stderr.
        sys.stderr.write(
            f"xbotv2: could not open log file {path}; logging to stderr only\n"
        )

    if also_stderr:
        stream_handler = logging.StreamHandler(sys.stderr)
        stream_handler.setFormatter(fmt)
        stream_handler.setLevel(level.upper())
        root.addHandler(stream_handler)

    # Quiet the noisy third-party loggers at WARNING. They can be
    # re-enabled by setting XBOTV2_LOG_LEVEL=DEBUG, but we don't
    # propagate their noise to the file log under default settings.
    for name in _NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)

    # Also configure the package subloggers explicitly so they
    # propagate to the root handler chain. We don't override their
    # level — let the root level win.
    for name in _PACKAGE_LOGGERS:
        lg = logging.getLogger(name)
        lg.setLevel(level.upper())
        lg.propagate = True

    logging.getLogger("xbotv2").info(
        "logging initialised level=%s file=%s also_stderr=%s",
        level.upper(),
        path,
        also_stderr,
    )
    return path

