"""Public reload operation and composition input owned by Loader."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from XBotv2.core.operations import EmptyRequest, Operation


@dataclass(frozen=True, slots=True)
class ReloadPlan:
    config_path: Path
    variables: dict[str, object]


@dataclass(frozen=True, slots=True)
class Reloaded:
    reloaded: tuple[str, ...]
    errors: tuple[str, ...]
    provider: str
    model: str
    model_mode: str
    context_window: int


RELOAD_PLUGINS = Operation(
    "loader/reload",
    EmptyRequest,
    Reloaded,
    exclusive=True,
)


__all__ = ["RELOAD_PLUGINS", "ReloadPlan", "Reloaded"]
