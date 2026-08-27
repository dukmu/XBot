"""Structured, content-safe runtime logging service."""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from typing import Any

_SENSITIVE_PARTS = (
    "authorization",
    "api_key",
    "cookie",
    "credential",
    "password",
    "secret",
)
_RUNTIME_CONTEXT: ContextVar[dict[str, Any]] = ContextVar(
    "xbot_runtime_log_context",
    default={},
)


def _is_sensitive(name: str) -> bool:
    lower_name = name.lower()
    return (
        any(part in lower_name for part in _SENSITIVE_PARTS)
        or lower_name == "token"
        or (lower_name.endswith("_token") and not lower_name.endswith("_tokens"))
    )


def _safe_value(name: str, value: Any) -> Any:
    if _is_sensitive(name):
        return "<redacted>"
    if isinstance(value, Mapping):
        return {
            str(key): _safe_value(str(key), nested)
            for key, nested in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_safe_value(name, nested) for nested in value]
    if isinstance(value, (set, frozenset)):
        return sorted((_safe_value(name, nested) for nested in value), key=str)
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    return str(value)


def _render_field(name: str, value: Any) -> str:
    rendered = json.dumps(
        _safe_value(name, value),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return rendered if len(rendered) <= 500 else rendered[:497] + "..."


def push_log_context(**fields: Any) -> Token[dict[str, Any]]:
    """Add correlation fields to the current asynchronous execution context."""
    present = {
        name: value
        for name, value in fields.items()
        if value is not None and value != ""
    }
    return _RUNTIME_CONTEXT.set({**_RUNTIME_CONTEXT.get(), **present})


def reset_log_context(token: Token[dict[str, Any]]) -> None:
    """Restore the correlation context returned by :func:`push_log_context`."""
    _RUNTIME_CONTEXT.reset(token)


def runtime_log_context() -> Mapping[str, Any]:
    """Return a snapshot of the current correlation fields."""
    return dict(_RUNTIME_CONTEXT.get())


def render_log_fields(fields: Mapping[str, Any]) -> str:
    """Render structured fields using the shared redaction and size policy."""
    return " ".join(
        f"{name}={_render_field(name, value)}"
        for name, value in sorted(fields.items())
    )


@dataclass(frozen=True, slots=True)
class RuntimeLog:
    """Bound logger exposed to plugins as the ``runtime_log`` XCore service."""

    category: str = "runtime"
    fields: dict[str, Any] = field(default_factory=dict)

    def bind(self, category: str | None = None, **fields: Any) -> "RuntimeLog":
        return RuntimeLog(
            category=category or self.category,
            fields={**self.fields, **fields},
        )

    def debug(self, event: str, **fields: Any) -> None:
        self._write(logging.DEBUG, event, fields)

    def info(self, event: str, **fields: Any) -> None:
        self._write(logging.INFO, event, fields)

    def warning(self, event: str, **fields: Any) -> None:
        self._write(logging.WARNING, event, fields)

    def error(self, event: str, **fields: Any) -> None:
        self._write(logging.ERROR, event, fields)

    def exception(self, event: str, **fields: Any) -> None:
        self._write(logging.ERROR, event, fields, exc_info=True)

    def log(self, level: int, event: str, **fields: Any) -> None:
        self._write(level, event, fields)

    def _write(
        self,
        level: int,
        event: str,
        fields: dict[str, Any],
        *,
        exc_info: bool = False,
    ) -> None:
        values = {
            name: value
            for name, value in {**self.fields, **fields}.items()
            if value is not None
        }
        suffix = render_log_fields(values)
        logging.getLogger(f"xbotv2.{self.category}").log(
            level,
            event if not suffix else f"{event} {suffix}",
            exc_info=exc_info,
            extra={"xbot_log_fields": frozenset(values)},
        )


DEFAULT_RUNTIME_LOG = RuntimeLog()


__all__ = [
    "DEFAULT_RUNTIME_LOG",
    "RuntimeLog",
    "push_log_context",
    "render_log_fields",
    "reset_log_context",
    "runtime_log_context",
]
