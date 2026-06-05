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
    - ``--log-level WARNING`` — only engine + dispatcher warnings.
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
    "xbotv2.dispatcher",
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
    "langgraph",
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
            delay=True,
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


def dump_conversation_state(
    logger: logging.Logger,
    *,
    data_dir: str | os.PathLike[str] | None = None,
    session_root: str | os.PathLike[str] | None = None,
    session_id: str,
    turn: int,
    messages: list[Any],
    last_response: Any = None,
    tool_calls: list[dict[str, Any]] | None = None,
    tool_messages: list[Any] | None = None,
    error: BaseException | None = None,
) -> Path:
    """Write a one-shot diagnostic dump to a file and return its
    path. The file is intended to be human-readable and
    post-mortem friendly: it captures the full conversation state
    right before a 400-style error, so the user can attach it to a
    bug report.

    Exactly one of ``data_dir`` or ``session_root`` should be
    provided. ``session_root`` (e.g. ``<data>/sessions/abc``) is
    the path the engine's state store already knows; we drop the
    dump at ``<session_root>/logs/dumps/`` so it lives next to
    ``events.jsonl`` and ``messages.jsonl``. ``data_dir`` writes
    to ``<data_dir>/logs/dumps/`` instead.

    A failure here must never break the surrounding engine
    call — we log any exception and return ``Path("")``.
    """

    if data_dir is None and session_root is None:
        raise ValueError("dump_conversation_state needs data_dir or session_root")

    if session_root is not None:
        dump_dir = Path(session_root) / "logs" / "dumps"
    else:
        dump_dir = Path(data_dir) / "logs" / "dumps"
    try:
        dump_dir.mkdir(parents=True, exist_ok=True)
        stamp = _timestamp()
        path = dump_dir / f"turn-{turn:04d}-{stamp}.txt"
        with path.open("w", encoding="utf-8") as fh:
            fh.write(f"# Diagnostic dump for session {session_id!r} turn {turn}\n")
            fh.write(f"# Generated by dump_conversation_state on {stamp}\n")
            if error is not None:
                fh.write(f"\n## Error\n{type(error).__name__}: {error}\n")
            fh.write("\n## Messages ({})\n".format(len(messages)))
            for index, message in enumerate(messages):
                fh.write(f"\n### [{index}] {type(message).__name__}\n")
                # ``content`` can be a string OR a list of content
                # blocks; render both shapes readably.
                content = getattr(message, "content", None)
                if isinstance(content, str):
                    fh.write(content + "\n")
                elif isinstance(content, list):
                    for block in content:
                        fh.write(f"  - {block!r}\n")
                else:
                    fh.write(repr(content) + "\n")
                tool_call_id = getattr(message, "tool_call_id", None)
                if tool_call_id:
                    fh.write(f"  tool_call_id: {tool_call_id}\n")
                tc = getattr(message, "tool_calls", None)
                if tc:
                    fh.write(f"  tool_calls: {tc!r}\n")
            if tool_calls is not None:
                fh.write(
                    f"\n## Tool calls parsed (n={len(tool_calls)})\n"
                )
                for tc in tool_calls:
                    fh.write(f"  - {tc!r}\n")
            if tool_messages is not None:
                fh.write(
                    f"\n## Tool messages (n={len(tool_messages)})\n"
                )
                for tm in tool_messages:
                    fh.write(
                        f"  - tool_call_id={getattr(tm, 'tool_call_id', None)!r} "
                        f"status={getattr(tm, 'status', None)!r}\n"
                    )
                    content = getattr(tm, "content", None)
                    if isinstance(content, str):
                        for line in content.splitlines() or [""]:
                            fh.write(f"      | {line}\n")
                    else:
                        fh.write(f"      | {content!r}\n")
            if last_response is not None:
                fh.write("\n## Last LLM response\n")
                fh.write(f"  type: {type(last_response).__name__}\n")
                fh.write(f"  content: {getattr(last_response, 'content', None)!r}\n")
                fh.write(f"  tool_calls: {getattr(last_response, 'tool_calls', None)!r}\n")
        logger.error(
            "engine: conversation dump written to %s "
            "(session=%s turn=%d messages=%d tool_calls=%d tool_messages=%d)",
            path,
            session_id,
            turn,
            len(messages),
            len(tool_calls or []),
            len(tool_messages or []),
        )
        return path
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "engine: failed to write conversation dump (session=%s turn=%d): %s",
            session_id,
            turn,
            exc,
        )
        return Path("")


def _timestamp() -> str:
    """Return a filesystem-safe timestamp like ``20260605T221534``."""

    from datetime import datetime

    return datetime.now().strftime("%Y%m%dT%H%M%S")
