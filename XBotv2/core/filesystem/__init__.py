"""Filesystem operation backend shared through stable operation contracts."""

from XBotv2.core.filesystem.operations import (
    PATH_ACCESS,
    execute,
    resolve_operation,
)

__all__ = ["PATH_ACCESS", "execute", "resolve_operation"]
