"""Rotating UTF-8 application logging."""

from __future__ import annotations

import logging
import logging.handlers
import os
import sys
import traceback
from collections.abc import Mapping
from pathlib import Path

from XBotv2.core.runtime_logging import render_log_fields, runtime_log_context

_DEFAULT_LOG_DIRNAME = "logs"
_DEFAULT_LOG_BASENAME = "xbotv2.log"
_MAX_BYTES = 5 * 1024 * 1024
_BACKUP_COUNT = 3

_OWNED_LOGGERS = ("xbotv2", "xcore")
_configured_category_loggers: set[str] = set()

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


class _RuntimeContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        included = getattr(record, "xbot_log_fields", frozenset())
        fields = {
            name: value
            for name, value in runtime_log_context().items()
            if name not in included
        }
        suffix = render_log_fields(fields)
        record.runtime_context = "" if not suffix else f" {suffix}"
        return True


class _ContentSafeFormatter(logging.Formatter):
    """Keep traceback locations while omitting exception message content."""

    def formatException(self, exc_info) -> str:  # noqa: N802
        exception_type, _error, trace = exc_info
        frames = "".join(
            f'  File "{frame.filename}", line {frame.lineno}, in {frame.name}\n'
            for frame in traceback.extract_tb(trace)
        )
        return "Traceback (most recent call last):\n" + frames + exception_type.__name__


def _category_levels(
    configured: Mapping[str, str] | None,
) -> dict[str, str]:
    values: dict[str, str] = {}
    raw = os.environ.get("XBOT_LOG_LEVELS", "")
    for entry in raw.split(","):
        if not entry.strip():
            continue
        name, separator, level = entry.partition("=")
        if not separator:
            raise ValueError("XBOT_LOG_LEVELS entries must use logger=LEVEL")
        values[name.strip()] = level.strip()
    values.update(configured or {})
    for name, level in values.items():
        if not any(name == root or name.startswith(f"{root}.") for root in _OWNED_LOGGERS):
            raise ValueError(f"Unsupported XBot log category: {name}")
        if level.upper() not in logging.getLevelNamesMapping():
            raise ValueError(f"Unsupported log level for {name}: {level}")
    return {name: level.upper() for name, level in values.items()}


def setup_logging(
    *,
    data_dir: str | os.PathLike[str] | None = None,
    level: str = "INFO",
    log_file: str | os.PathLike[str] | None = None,
    also_stderr: bool | None = None,
    category_levels: Mapping[str, str] | None = None,
) -> Path:
    """Configure owned loggers and return the selected log path."""

    if log_file is not None:
        path = Path(log_file).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
    else:
        path = _resolve_log_file(data_dir)

    if also_stderr is None:
        also_stderr = False

    global _configured_category_loggers
    levels = _category_levels(category_levels)
    handler_level = min(
        logging.getLevelNamesMapping()[level.upper()],
        *(logging.getLevelNamesMapping()[value] for value in levels.values()),
    ) if levels else logging.getLevelNamesMapping()[level.upper()]
    for name in _configured_category_loggers:
        logging.getLogger(name).setLevel(logging.NOTSET)
    _configured_category_loggers = set(levels)

    roots = [logging.getLogger(name) for name in _OWNED_LOGGERS]
    previous_handlers = {
        handler for root in roots for handler in root.handlers
    }
    for root in roots:
        for handler in list(root.handlers):
            root.removeHandler(handler)
        root.setLevel(level.upper())
        root.propagate = True
    for handler in previous_handlers:
        handler.close()

    fmt = _ContentSafeFormatter(
        fmt=(
            "%(asctime)s.%(msecs)03d %(levelname)-7s %(name)s: "
            "%(message)s%(runtime_context)s"
        ),
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    context_filter = _RuntimeContextFilter()

    try:
        file_handler: logging.Handler = logging.handlers.RotatingFileHandler(
            path,
            maxBytes=_MAX_BYTES,
            backupCount=_BACKUP_COUNT,
            encoding="utf-8",
            delay=False,
        )
        file_handler.setFormatter(fmt)
        file_handler.setLevel(handler_level)
        file_handler.addFilter(context_filter)
        for root in roots:
            root.addHandler(file_handler)
    except OSError:
        sys.stderr.write(
            f"xbotv2: could not open log file {path}; logging to stderr only\n"
        )

        also_stderr = True

    if also_stderr:
        stream_handler = logging.StreamHandler(sys.stderr)
        stream_handler.setFormatter(fmt)
        stream_handler.setLevel(handler_level)
        stream_handler.addFilter(context_filter)
        for root in roots:
            root.addHandler(stream_handler)

    for name in _NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
    for name, category_level in levels.items():
        logging.getLogger(name).setLevel(category_level)

    logging.getLogger("xbotv2").info(
        "logging initialised level=%s file=%s also_stderr=%s category_levels=%s",
        level.upper(),
        path,
        also_stderr,
        levels,
    )
    return path
