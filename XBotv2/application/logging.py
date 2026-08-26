"""Rotating UTF-8 application logging."""

from __future__ import annotations

import logging
import logging.handlers
import os
import sys
from pathlib import Path
from typing import Any

_DEFAULT_LOG_DIRNAME = "logs"
_DEFAULT_LOG_BASENAME = "xbotv2.log"
_MAX_BYTES = 5 * 1024 * 1024
_BACKUP_COUNT = 3

_PACKAGE_LOGGERS = (
    "xbotv2",
    "xbotv2.engine",
    "xbotv2.http_server",
    "xbotv2.server",
    "tools",
    "tui",
    "protocol",
    "core",
)

_NOISY_LOGGERS = (
    "uvicorn",
    "uvicorn.error",
    "uvicorn.access",
    "httpx",
    "httpcore",
    "starlette",
)


def _resolve_log_file(data_dir: str | os.PathLike[str] | None) -> Path:
    """Return the absolute path of the log file; create its directory."""

    env = os.environ.get("XBOT_LOG_FILE")
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
    """Configure owned loggers and return the selected log path."""

    if log_file is not None:
        path = Path(log_file).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
    else:
        path = _resolve_log_file(data_dir)

    if also_stderr is None:
        also_stderr = False

    root = logging.getLogger("xbotv2")
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
        sys.stderr.write(
            f"xbotv2: could not open log file {path}; logging to stderr only\n"
        )

    if also_stderr:
        stream_handler = logging.StreamHandler(sys.stderr)
        stream_handler.setFormatter(fmt)
        stream_handler.setLevel(level.upper())
        root.addHandler(stream_handler)

    for name in _NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)

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
